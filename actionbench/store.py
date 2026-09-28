from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import Config
from .errors import ResumeConflict


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def migrate(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
          campaign TEXT PRIMARY KEY, config_hash TEXT NOT NULL, config_json TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, frozen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS episodes (
          episode_id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(campaign),
          task_id TEXT NOT NULL, family TEXT NOT NULL, condition TEXT NOT NULL, replica INTEGER NOT NULL,
          skill_hash TEXT, status TEXT NOT NULL, claimed_by TEXT, claim_expires_at TEXT,
          final_artifact TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(campaign, task_id, condition, replica, skill_hash)
        );
        CREATE TABLE IF NOT EXISTS requests (
          request_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
          step_id TEXT NOT NULL, provider_request_id TEXT, state TEXT NOT NULL,
          request_json TEXT NOT NULL, response_json TEXT, reserved_usd REAL NOT NULL,
          actual_usd REAL, input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
          created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(episode_id, step_id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT REFERENCES episodes(episode_id),
          kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluations (
          episode_id TEXT PRIMARY KEY REFERENCES episodes(episode_id), grader TEXT NOT NULL,
          score_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS skill_packages (
          campaign TEXT NOT NULL REFERENCES campaigns(campaign), family TEXT NOT NULL, replica INTEGER NOT NULL,
          package_hash TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(campaign, family, replica)
        );
        """)

    def ensure_campaign(self, config: Config) -> None:
        payload = config.source_path.read_text()
        row = self.conn.execute("SELECT config_hash FROM campaigns WHERE campaign=?", (config.campaign,)).fetchone()
        if row and row["config_hash"] != config.fingerprint:
            raise ResumeConflict("Campaign exists with a different configuration. Choose a new campaign name.")
        if not row:
            self.conn.execute("INSERT INTO campaigns VALUES (?, ?, ?, 'draft', ?, NULL)",
                              (config.campaign, config.fingerprint, payload, now()))

    def event(self, episode_id: str | None, kind: str, payload: dict) -> None:
        self.conn.execute("INSERT INTO events(episode_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                          (episode_id, kind, json.dumps(payload, sort_keys=True), now()))

    def create_episode(self, episode_id: str, campaign: str, task_id: str, family: str, condition: str, replica: int, skill_hash: str | None = None) -> None:
        stamp = now()
        self.conn.execute("""INSERT OR IGNORE INTO episodes(episode_id,campaign,task_id,family,condition,replica,skill_hash,status,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,'queued',?,?)""", (episode_id, campaign, task_id, family, condition, replica, skill_hash, stamp, stamp))

    def save_skill_package(self, campaign: str, family: str, replica: int, package_hash: str, path: str) -> None:
        self.conn.execute("INSERT INTO skill_packages VALUES(?,?,?,?,?,?)", (campaign, family, replica, package_hash, path, now()))

    def skill_package(self, campaign: str, family: str, replica: int):
        return self.conn.execute("SELECT * FROM skill_packages WHERE campaign=? AND family=? AND replica=?", (campaign, family, replica)).fetchone()

    def episode(self, episode_id: str):
        return self.conn.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()

    def set_episode(self, episode_id: str, status: str, *, final_artifact: str | None = None, error: str | None = None) -> None:
        self.conn.execute("UPDATE episodes SET status=?, final_artifact=COALESCE(?,final_artifact), error=?, updated_at=? WHERE episode_id=?", (status, final_artifact, error, now(), episode_id))

    def pending_episodes(self, campaign: str):
        return self.conn.execute("SELECT * FROM episodes WHERE campaign=? AND status IN ('queued','running') ORDER BY task_id, condition, replica", (campaign,)).fetchall()

    def save_evaluation(self, episode_id: str, grader: str, score: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?)", (episode_id, grader, json.dumps(score, sort_keys=True), now()))

    def campaign_status(self, campaign: str) -> dict:
        campaign_row = self.conn.execute("SELECT * FROM campaigns WHERE campaign=?", (campaign,)).fetchone()
        if not campaign_row:
            return {"campaign": campaign, "exists": False}
        counts = self.conn.execute("SELECT status,count(*) n FROM episodes WHERE campaign=? GROUP BY status", (campaign,)).fetchall()
        spend = self.conn.execute("SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) v FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE e.campaign=?", (campaign,)).fetchone()["v"]
        return {"campaign": campaign, "exists": True, "status": campaign_row["status"], "episodes": {r["status"]: r["n"] for r in counts}, "accounted_usd": spend}

    def campaign_spend(self, campaign: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) v FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE e.campaign=?", (campaign,)).fetchone()
        return float(row["v"])

    def request_for_step(self, episode_id: str, step_id: str):
        return self.conn.execute("SELECT * FROM requests WHERE episode_id=? AND step_id=?", (episode_id, step_id)).fetchone()

    def reserve_request(self, request_id: str, episode_id: str, step_id: str, request_json: dict, reserved_usd: float) -> None:
        with self.tx() as conn:
            conn.execute("INSERT INTO requests(request_id,episode_id,step_id,state,request_json,reserved_usd,created_at) VALUES(?,?,?,'reserved',?,?,?)",
                         (request_id, episode_id, step_id, json.dumps(request_json, sort_keys=True), reserved_usd, now()))

    def mark_submitted(self, request_id: str) -> None:
        self.conn.execute("UPDATE requests SET state='submitted' WHERE request_id=? AND state='reserved'", (request_id,))

    def complete_request(self, request_id: str, provider_request_id: str | None, response: dict, usage: dict, actual_usd: float) -> None:
        with self.tx() as conn:
            conn.execute("""UPDATE requests SET state='completed', provider_request_id=?, response_json=?, actual_usd=?,
                         input_tokens=?, cached_input_tokens=?, output_tokens=?, completed_at=? WHERE request_id=? AND state='submitted'""",
                         (provider_request_id, json.dumps(response, sort_keys=True), actual_usd, usage.get("input_tokens", 0),
                          usage.get("cached_input_tokens", 0), usage.get("output_tokens", 0), now(), request_id))

    def unknown_request(self, request_id: str, detail: str) -> None:
        self.conn.execute("UPDATE requests SET state='unknown_outcome', completed_at=? WHERE request_id=? AND state='submitted'", (now(), request_id))
        self.event(None, "unknown_provider_outcome", {"request_id": request_id, "detail": detail})
