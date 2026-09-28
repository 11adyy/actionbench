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
    """Persistent source of truth for a campaign, its calls, and its action steps."""

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
          package_hash TEXT, status TEXT NOT NULL, retryable INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
          final_artifact TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(campaign,task_id,condition,replica,package_hash)
        );
        CREATE TABLE IF NOT EXISTS requests (
          request_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
          request_key TEXT NOT NULL, request_hash TEXT NOT NULL, provider_request_id TEXT, state TEXT NOT NULL,
          request_json TEXT NOT NULL, response_json TEXT, reserved_usd REAL NOT NULL,
          reserved_input_tokens INTEGER NOT NULL, actual_usd REAL, input_tokens INTEGER,
          cached_input_tokens INTEGER, output_tokens INTEGER, created_at TEXT NOT NULL, completed_at TEXT,
          UNIQUE(episode_id,request_key,request_hash)
        );
        CREATE TABLE IF NOT EXISTS action_runs (
          action_run_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
          invocation_key TEXT NOT NULL, action_id TEXT NOT NULL, input_hash TEXT NOT NULL,
          workspace TEXT NOT NULL, state TEXT NOT NULL, output_json TEXT, error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(episode_id,invocation_key,input_hash)
        );
        CREATE TABLE IF NOT EXISTS generated_packages (
          campaign TEXT NOT NULL REFERENCES campaigns(campaign), family TEXT NOT NULL, replica INTEGER NOT NULL,
          condition TEXT NOT NULL CHECK(condition IN ('skill','skill_script','action')), package_hash TEXT NOT NULL,
          path TEXT NOT NULL, creation_episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
          created_at TEXT NOT NULL, PRIMARY KEY(campaign,family,replica,condition)
        );
        CREATE TABLE IF NOT EXISTS evaluations (
          episode_id TEXT PRIMARY KEY REFERENCES episodes(episode_id), grader TEXT NOT NULL,
          score_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT REFERENCES episodes(episode_id),
          kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # The first prototype used similarly named columns. Keep its ledger readable
        # rather than silently creating a second database during an interrupted study.
        for statement in (
            "ALTER TABLE episodes ADD COLUMN package_hash TEXT",
            "ALTER TABLE episodes ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE episodes ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE requests ADD COLUMN request_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE requests ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE requests ADD COLUMN reserved_input_tokens INTEGER NOT NULL DEFAULT 0",
        ):
            try: self.conn.execute(statement)
            except sqlite3.OperationalError: pass
        try:
            self.conn.execute("UPDATE episodes SET package_hash=skill_hash WHERE package_hash IS NULL AND skill_hash IS NOT NULL")
        except sqlite3.OperationalError:
            pass

    def ensure_campaign(self, config: Config) -> None:
        payload = config.source_path.read_text()
        row = self.conn.execute("SELECT config_hash FROM campaigns WHERE campaign=?", (config.campaign,)).fetchone()
        if row and row["config_hash"] != config.fingerprint:
            raise ResumeConflict("Campaign exists with a different configuration. Choose a new campaign name.")
        if not row:
            self.conn.execute("INSERT INTO campaigns VALUES (?, ?, ?, 'draft', ?, NULL)", (config.campaign, config.fingerprint, payload, now()))

    def assert_mutable(self, campaign: str) -> None:
        row = self.conn.execute("SELECT status FROM campaigns WHERE campaign=?", (campaign,)).fetchone()
        if not row: raise ResumeConflict(f"Campaign does not exist: {campaign}")
        if row["status"] == "frozen": raise ResumeConflict("Campaign is frozen; create a new campaign to change it")

    def event(self, episode_id: str | None, kind: str, payload: dict) -> None:
        self.conn.execute("INSERT INTO events(episode_id,kind,payload_json,created_at) VALUES(?,?,?,?)", (episode_id, kind, json.dumps(payload, sort_keys=True), now()))

    def create_episode(self, episode_id: str, campaign: str, task_id: str, family: str, condition: str, replica: int, package_hash: str | None = None) -> None:
        self.assert_mutable(campaign)
        stamp = now()
        self.conn.execute("""INSERT OR IGNORE INTO episodes(episode_id,campaign,task_id,family,condition,replica,package_hash,status,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,'queued',?,?)""", (episode_id, campaign, task_id, family, condition, replica, package_hash, stamp, stamp))

    def episode(self, episode_id: str):
        return self.conn.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()

    def set_episode(self, episode_id: str, status: str, *, final_artifact: str | None = None, error: str | None = None, retryable: bool = True) -> None:
        if status == "running":
            self.conn.execute("UPDATE episodes SET status=?,attempts=attempts+1,error=?,updated_at=? WHERE episode_id=?", (status, error, now(), episode_id))
            return
        if status == "failed" and retryable:
            row = self.episode(episode_id)
            retryable = bool(row and row["attempts"] < 2)
        self.conn.execute("UPDATE episodes SET status=?,retryable=?,final_artifact=COALESCE(?,final_artifact),error=?,updated_at=? WHERE episode_id=?", (status, int(retryable), final_artifact, error, now(), episode_id))

    def resumable_episodes(self, campaign: str):
        self.conn.execute("""UPDATE episodes SET status='failed',retryable=0,error=COALESCE(error,'Interrupted twice before completion'),updated_at=?
                           WHERE campaign=? AND status='running' AND attempts>=2""", (now(), campaign))
        return self.conn.execute("SELECT * FROM episodes WHERE campaign=? AND (status='queued' OR (status='running' AND attempts<2) OR (status='failed' AND retryable=1 AND attempts<2)) ORDER BY task_id,condition,replica", (campaign,)).fetchall()

    def save_package(self, campaign: str, family: str, replica: int, condition: str, package_hash: str, path: str, creation_episode_id: str) -> None:
        self.conn.execute("INSERT INTO generated_packages VALUES(?,?,?,?,?,?,?,?)", (campaign, family, replica, condition, package_hash, path, creation_episode_id, now()))

    def package(self, campaign: str, family: str, replica: int, condition: str):
        return self.conn.execute("SELECT * FROM generated_packages WHERE campaign=? AND family=? AND replica=? AND condition=?", (campaign, family, replica, condition)).fetchone()

    def evaluation(self, episode_id: str):
        return self.conn.execute("SELECT * FROM evaluations WHERE episode_id=?", (episode_id,)).fetchone()

    def request_for(self, episode_id: str, request_key: str, request_hash: str):
        return self.conn.execute("SELECT * FROM requests WHERE episode_id=? AND request_key=? AND request_hash=?", (episode_id, request_key, request_hash)).fetchone()

    def reserve_request(self, request_id: str, episode_id: str, request_key: str, request_hash: str, request_json: dict, reserved_usd: float, reserved_input_tokens: int) -> None:
        with self.tx() as conn:
            conn.execute("""INSERT INTO requests(request_id,episode_id,request_key,request_hash,state,request_json,reserved_usd,reserved_input_tokens,created_at)
                         VALUES(?,?,?,?, 'reserved',?,?,?,?)""", (request_id, episode_id, request_key, request_hash, json.dumps(request_json, sort_keys=True), reserved_usd, reserved_input_tokens, now()))

    def mark_submitted(self, request_id: str) -> None:
        self.conn.execute("UPDATE requests SET state='submitted' WHERE request_id=? AND state='reserved'", (request_id,))

    def complete_request(self, request_id: str, provider_request_id: str | None, response: dict, usage: dict, actual_usd: float) -> None:
        with self.tx() as conn:
            conn.execute("""UPDATE requests SET state='completed',provider_request_id=?,response_json=?,actual_usd=?,input_tokens=?,cached_input_tokens=?,output_tokens=?,completed_at=?
                         WHERE request_id=? AND state='submitted'""", (provider_request_id, json.dumps(response, sort_keys=True), actual_usd, usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0), usage.get("output_tokens", 0), now(), request_id))

    def unknown_request(self, request_id: str, detail: str) -> None:
        self.conn.execute("UPDATE requests SET state='unknown_outcome',completed_at=? WHERE request_id=? AND state='submitted'", (now(), request_id))
        self.event(None, "unknown_provider_outcome", {"request_id": request_id, "detail": detail})

    def episode_limits(self, episode_id: str) -> tuple[int, int, int]:
        row = self.conn.execute("""SELECT COUNT(*) calls,COALESCE(SUM(COALESCE(input_tokens,reserved_input_tokens)),0) inputs,COALESCE(SUM(output_tokens),0) outputs
                                   FROM requests WHERE episode_id=? AND state IN ('reserved','submitted','completed','unknown_outcome')""", (episode_id,)).fetchone()
        return int(row["calls"]), int(row["inputs"]), int(row["outputs"])

    def campaign_spend(self, campaign: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) value FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE e.campaign=?", (campaign,)).fetchone()
        return float(row["value"])

    def create_action_run(self, action_run_id: str, episode_id: str, invocation_key: str, action_id: str, input_hash: str, workspace: str):
        stamp = now()
        self.conn.execute("""INSERT OR IGNORE INTO action_runs(action_run_id,episode_id,invocation_key,action_id,input_hash,workspace,state,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,'queued',?,?)""", (action_run_id, episode_id, invocation_key, action_id, input_hash, workspace, stamp, stamp))
        return self.conn.execute("SELECT * FROM action_runs WHERE action_run_id=?", (action_run_id,)).fetchone()

    def action_run(self, action_run_id: str):
        return self.conn.execute("SELECT * FROM action_runs WHERE action_run_id=?", (action_run_id,)).fetchone()

    def set_action_run(self, action_run_id: str, state: str, *, output: dict | None = None, error: str | None = None) -> None:
        self.conn.execute("UPDATE action_runs SET state=?,output_json=COALESCE(?,output_json),error=?,updated_at=? WHERE action_run_id=?", (state, json.dumps(output, sort_keys=True) if output is not None else None, error, now(), action_run_id))

    def save_evaluation(self, episode_id: str, grader: str, score: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?)", (episode_id, grader, json.dumps(score, sort_keys=True), now()))

    def campaign_status(self, campaign: str) -> dict:
        row = self.conn.execute("SELECT status FROM campaigns WHERE campaign=?", (campaign,)).fetchone()
        if not row: return {"campaign": campaign, "exists": False}
        counts = self.conn.execute("SELECT status,COUNT(*) n FROM episodes WHERE campaign=? GROUP BY status", (campaign,)).fetchall()
        return {"campaign": campaign, "exists": True, "status": row["status"], "episodes": {r["status"]: r["n"] for r in counts}, "accounted_usd": self.campaign_spend(campaign)}
