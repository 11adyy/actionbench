from __future__ import annotations

import json
import hashlib
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
        CREATE TABLE IF NOT EXISTS campaign_inputs (
          campaign TEXT PRIMARY KEY REFERENCES campaigns(campaign), manifest_hash TEXT NOT NULL,
          harness_hash TEXT NOT NULL, image_hashes_json TEXT NOT NULL,
          planned_test_episodes INTEGER NOT NULL, bound_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS validation_gates (
          campaign TEXT NOT NULL REFERENCES campaigns(campaign), kind TEXT NOT NULL,
          manifest_hash TEXT NOT NULL, harness_hash TEXT NOT NULL,
          image_hashes_json TEXT NOT NULL, result_json TEXT NOT NULL,
          completed_at TEXT NOT NULL, PRIMARY KEY(campaign,kind)
        );
        CREATE TABLE IF NOT EXISTS budget_updates (
          id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL REFERENCES campaigns(campaign),
          ceiling_usd REAL NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS episodes (
          episode_id TEXT PRIMARY KEY, campaign TEXT NOT NULL REFERENCES campaigns(campaign),
          task_id TEXT NOT NULL, family TEXT NOT NULL, condition TEXT NOT NULL, replica INTEGER NOT NULL,
          package_hash TEXT, status TEXT NOT NULL, retryable INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
          duration_seconds REAL NOT NULL DEFAULT 0, failure_kind TEXT,
          final_artifact TEXT, final_artifact_sha256 TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(campaign,task_id,condition,replica,package_hash)
        );
        CREATE TABLE IF NOT EXISTS requests (
          request_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
          request_key TEXT NOT NULL, request_hash TEXT NOT NULL, provider_request_id TEXT, state TEXT NOT NULL,
          request_json TEXT NOT NULL, response_json TEXT, reserved_usd REAL NOT NULL,
          reserved_input_tokens INTEGER NOT NULL, actual_usd REAL, input_tokens INTEGER,
          cached_input_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER,
          response_status TEXT, incomplete_reason TEXT, model_returned TEXT, output_validation TEXT,
          created_at TEXT NOT NULL, completed_at TEXT,
          UNIQUE(episode_id,request_key,request_hash)
        );
        CREATE TABLE IF NOT EXISTS action_runs (
          action_run_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
          invocation_key TEXT NOT NULL, action_id TEXT NOT NULL, input_hash TEXT NOT NULL,
          workspace TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, output_json TEXT, error TEXT,
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
            "ALTER TABLE episodes ADD COLUMN duration_seconds REAL NOT NULL DEFAULT 0",
            "ALTER TABLE episodes ADD COLUMN failure_kind TEXT",
            "ALTER TABLE episodes ADD COLUMN final_artifact_sha256 TEXT",
            "ALTER TABLE requests ADD COLUMN request_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE requests ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE requests ADD COLUMN reserved_input_tokens INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE requests ADD COLUMN cache_write_tokens INTEGER",
            "ALTER TABLE requests ADD COLUMN response_status TEXT",
            "ALTER TABLE requests ADD COLUMN incomplete_reason TEXT",
            "ALTER TABLE requests ADD COLUMN model_returned TEXT",
            "ALTER TABLE requests ADD COLUMN output_validation TEXT",
            "ALTER TABLE action_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
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

    def bind_study(self, campaign: str, manifest_hash: str, harness_hash: str, image_hashes: dict[str, str], planned_test_episodes: int) -> None:
        payload = json.dumps(image_hashes, sort_keys=True)
        row = self.conn.execute("SELECT * FROM campaign_inputs WHERE campaign=?", (campaign,)).fetchone()
        if row:
            if (row["manifest_hash"], row["harness_hash"], row["image_hashes_json"], row["planned_test_episodes"]) != (manifest_hash, harness_hash, payload, planned_test_episodes):
                raise ResumeConflict("Manifest, harness, or container image changed after campaign binding; start a new campaign")
            return
        self.assert_mutable(campaign)
        self.conn.execute("INSERT INTO campaign_inputs VALUES (?,?,?,?,?,?)", (campaign, manifest_hash, harness_hash, payload, planned_test_episodes, now()))

    def study_binding(self, campaign: str):
        return self.conn.execute("SELECT * FROM campaign_inputs WHERE campaign=?", (campaign,)).fetchone()

    def record_gate(self, campaign: str, kind: str, manifest_hash: str, harness_hash: str, image_hashes: dict[str, str], result: dict) -> None:
        self.assert_mutable(campaign)
        self.conn.execute("""INSERT OR REPLACE INTO validation_gates VALUES(?,?,?,?,?,?,?)""",
                          (campaign, kind, manifest_hash, harness_hash, json.dumps(image_hashes, sort_keys=True), json.dumps(result, sort_keys=True), now()))

    def gate(self, campaign: str, kind: str):
        return self.conn.execute("SELECT * FROM validation_gates WHERE campaign=? AND kind=?", (campaign, kind)).fetchone()

    def budget_ceiling(self, config: Config) -> float:
        row = self.conn.execute("SELECT MAX(ceiling_usd) ceiling FROM budget_updates WHERE campaign=?", (config.campaign,)).fetchone()
        return max(config.budget.usd, float(row["ceiling"])) if row and row["ceiling"] is not None else config.budget.usd

    def raise_budget_ceiling(self, config: Config, ceiling_usd: float) -> float:
        self.assert_mutable(config.campaign)
        if ceiling_usd <= self.budget_ceiling(config): raise ResumeConflict("New campaign ceiling must exceed the current ceiling")
        self.conn.execute("INSERT INTO budget_updates(campaign,ceiling_usd,created_at) VALUES(?,?,?)", (config.campaign, ceiling_usd, now()))
        return ceiling_usd

    def event(self, episode_id: str | None, kind: str, payload: dict) -> None:
        self.conn.execute("INSERT INTO events(episode_id,kind,payload_json,created_at) VALUES(?,?,?,?)", (episode_id, kind, json.dumps(payload, sort_keys=True), now()))

    def create_episode(self, episode_id: str, campaign: str, task_id: str, family: str, condition: str, replica: int, package_hash: str | None = None) -> None:
        self.assert_mutable(campaign)
        stamp = now()
        self.conn.execute("""INSERT OR IGNORE INTO episodes(episode_id,campaign,task_id,family,condition,replica,package_hash,status,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,'queued',?,?)""", (episode_id, campaign, task_id, family, condition, replica, package_hash, stamp, stamp))

    def episode(self, episode_id: str):
        return self.conn.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()

    def set_episode(self, episode_id: str, status: str, *, final_artifact: str | None = None, error: str | None = None, retryable: bool = True, failure_kind: str | None = None) -> None:
        if status == "running":
            self.conn.execute("UPDATE episodes SET status=?,attempts=attempts+1,error=?,failure_kind=NULL,updated_at=? WHERE episode_id=?", (status, error, now(), episode_id))
            return
        self.conn.execute("UPDATE episodes SET status=?,retryable=?,final_artifact=COALESCE(?,final_artifact),error=?,failure_kind=?,updated_at=? WHERE episode_id=?", (status, int(retryable), final_artifact, error, failure_kind, now(), episode_id))

    def save_answer(self, episode_id: str, path: str) -> None:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.conn.execute("UPDATE episodes SET final_artifact=?,final_artifact_sha256=?,updated_at=? WHERE episode_id=?", (path, digest, now(), episode_id))

    def add_episode_duration(self, episode_id: str, seconds: float) -> None:
        self.conn.execute("UPDATE episodes SET duration_seconds=duration_seconds+? WHERE episode_id=?", (max(0, seconds), episode_id))

    def resumable_episodes(self, campaign: str):
        return self.conn.execute("SELECT * FROM episodes WHERE campaign=? AND (status IN ('queued','running') OR (status='failed' AND retryable=1)) ORDER BY task_id,condition,replica", (campaign,)).fetchall()

    def save_package(self, campaign: str, family: str, replica: int, condition: str, package_hash: str, path: str, creation_episode_id: str) -> None:
        self.conn.execute("INSERT INTO generated_packages VALUES(?,?,?,?,?,?,?,?)", (campaign, family, replica, condition, package_hash, path, creation_episode_id, now()))

    def package(self, campaign: str, family: str, replica: int, condition: str):
        return self.conn.execute("SELECT * FROM generated_packages WHERE campaign=? AND family=? AND replica=? AND condition=?", (campaign, family, replica, condition)).fetchone()

    def evaluation(self, episode_id: str):
        return self.conn.execute("SELECT * FROM evaluations WHERE episode_id=?", (episode_id,)).fetchone()

    def request_for(self, episode_id: str, request_key: str, request_hash: str):
        return self.conn.execute("SELECT * FROM requests WHERE episode_id=? AND request_key=? AND request_hash=?", (episode_id, request_key, request_hash)).fetchone()

    def request_by_id(self, request_id: str):
        return self.conn.execute("SELECT r.*,e.campaign,e.status episode_status FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE r.request_id=?", (request_id,)).fetchone()

    def reconcile_request(self, campaign: str, request_id: str, *, evidence: str, response: dict | None = None, usage: dict | None = None, actual_usd: float | None = None) -> None:
        self.assert_mutable(campaign)
        if not evidence.strip(): raise ResumeConflict("Reconciliation requires an evidence reference")
        with self.tx() as conn:
            row = conn.execute("SELECT r.*,e.campaign,e.status episode_status,e.task_id,e.family,e.condition,e.replica FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE r.request_id=?", (request_id,)).fetchone()
            if not row or row["campaign"] != campaign or row["state"] != "unknown_outcome" or row["episode_status"] != "blocked":
                raise ResumeConflict("Request must have an unknown outcome in a blocked episode of this campaign")
            if response is None:
                if row["response_json"] is not None:
                    raise ResumeConflict("A provider response was received; it cannot be marked not executed")
                conn.execute("UPDATE requests SET state='reserved',completed_at=NULL WHERE request_id=?", (request_id,))
                resolution = "confirmed_not_executed"
            else:
                conn.execute("""UPDATE requests SET state='completed',provider_request_id=?,response_json=?,actual_usd=?,input_tokens=?,cached_input_tokens=?,cache_write_tokens=?,output_tokens=?,response_status=?,incomplete_reason=?,model_returned=?,completed_at=? WHERE request_id=?""",
                             (response.get("id"), json.dumps(response, sort_keys=True), actual_usd, usage["input_tokens"], usage["cached_input_tokens"], usage.get("cache_write_tokens", 0), usage["output_tokens"], response.get("status"), (response.get("incomplete_details") or {}).get("reason"), response.get("model"), now(), request_id))
                resolution = "provider_response_recovered"
            conn.execute("UPDATE episodes SET status='queued',retryable=1,error=NULL,updated_at=? WHERE episode_id=?", (now(), row["episode_id"]))
            if row["task_id"].startswith("creation-dev:"):
                parent_task = f"creation:{row['family']}:{row['condition']}:{row['replica']}"
                conn.execute("UPDATE episodes SET status='queued',retryable=1,error=NULL,updated_at=? WHERE campaign=? AND task_id=? AND status='blocked'", (now(), campaign, parent_task))
            conn.execute("INSERT INTO events(episode_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                         (row["episode_id"], "manual_provider_reconciliation", json.dumps({"request_id": request_id, "resolution": resolution, "evidence": evidence}, sort_keys=True), now()))

    def reserve_request(self, request_id: str, episode_id: str, request_key: str, request_hash: str, request_json: dict, reserved_usd: float, reserved_input_tokens: int) -> None:
        with self.tx() as conn:
            conn.execute("""INSERT INTO requests(request_id,episode_id,request_key,request_hash,state,request_json,reserved_usd,reserved_input_tokens,created_at)
                         VALUES(?,?,?,?, 'reserved',?,?,?,?)""", (request_id, episode_id, request_key, request_hash, json.dumps(request_json, sort_keys=True), reserved_usd, reserved_input_tokens, now()))

    def mark_submitted(self, request_id: str) -> None:
        self.conn.execute("UPDATE requests SET state='submitted' WHERE request_id=? AND state IN ('reserved','rejected')", (request_id,))

    def reject_request(self, request_id: str, detail: str, *, policy: bool = False) -> None:
        self.conn.execute("UPDATE requests SET state=?,completed_at=? WHERE request_id=? AND state='submitted'", ("policy_rejected" if policy else "rejected", now(), request_id))
        self.event(None, "provider_rejection", {"request_id": request_id, "detail": detail})

    def complete_request(self, request_id: str, provider_request_id: str | None, response: dict, usage: dict, actual_usd: float) -> None:
        with self.tx() as conn:
            conn.execute("""UPDATE requests SET state='completed',provider_request_id=?,response_json=?,actual_usd=?,input_tokens=?,cached_input_tokens=?,cache_write_tokens=?,output_tokens=?,response_status=?,incomplete_reason=?,model_returned=?,completed_at=?
                         WHERE request_id=? AND state='submitted'""", (provider_request_id, json.dumps(response, sort_keys=True), actual_usd, usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0), usage.get("cache_write_tokens", 0), usage.get("output_tokens", 0), response.get("status"), (response.get("incomplete_details") or {}).get("reason"), response.get("model"), now(), request_id))

    def mark_output_validation(self, request_id: str, status: str) -> None:
        if status not in {"provider_complete", "accepted", "invalid"}:
            raise ValueError("Unknown output validation status")
        if status == "provider_complete":
            self.conn.execute("UPDATE requests SET output_validation=? WHERE request_id=? AND state='completed' AND output_validation IS NULL", (status, request_id))
        else:
            self.conn.execute("UPDATE requests SET output_validation=? WHERE request_id=? AND state='completed'", (status, request_id))

    def unknown_request(self, request_id: str, detail: str) -> None:
        self.conn.execute("UPDATE requests SET state='unknown_outcome',completed_at=? WHERE request_id=? AND state='submitted'", (now(), request_id))
        self.event(None, "unknown_provider_outcome", {"request_id": request_id, "detail": detail})

    def preserve_unpriced_response(self, request_id: str, response: object, detail: str) -> None:
        """Keep a received response even when its usage cannot be reconciled."""
        fields = response if isinstance(response, dict) else {}
        incomplete = fields.get("incomplete_details") if isinstance(fields.get("incomplete_details"), dict) else {}
        with self.tx() as conn:
            conn.execute("""UPDATE requests SET state='unknown_outcome',provider_request_id=?,response_json=?,
                           response_status=?,incomplete_reason=?,model_returned=?,completed_at=?
                           WHERE request_id=? AND state='submitted'""",
                         (fields.get("id"), json.dumps(response, sort_keys=True), fields.get("status"),
                          incomplete.get("reason"), fields.get("model"), now(), request_id))
            conn.execute("INSERT INTO events(episode_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                         (None, "provider_usage_review", json.dumps({"request_id": request_id, "detail": detail}, sort_keys=True), now()))

    def episode_limits(self, episode_id: str) -> tuple[int, int, int]:
        row = self.conn.execute("""SELECT COUNT(*) calls,COALESCE(SUM(COALESCE(input_tokens,reserved_input_tokens)),0) inputs,COALESCE(SUM(output_tokens),0) outputs
                                   FROM requests WHERE episode_id=? AND state IN ('reserved','submitted','completed','unknown_outcome')""", (episode_id,)).fetchone()
        return int(row["calls"]), int(row["inputs"]), int(row["outputs"])

    def campaign_spend(self, campaign: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) value FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE e.campaign=? AND r.state NOT IN ('rejected','policy_rejected')", (campaign,)).fetchone()
        return float(row["value"])

    def create_action_run(self, action_run_id: str, episode_id: str, invocation_key: str, action_id: str, input_hash: str, workspace: str):
        stamp = now()
        self.conn.execute("""INSERT OR IGNORE INTO action_runs(action_run_id,episode_id,invocation_key,action_id,input_hash,workspace,state,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,'queued',?,?)""", (action_run_id, episode_id, invocation_key, action_id, input_hash, workspace, stamp, stamp))
        return self.conn.execute("SELECT * FROM action_runs WHERE action_run_id=?", (action_run_id,)).fetchone()

    def action_run(self, action_run_id: str):
        return self.conn.execute("SELECT * FROM action_runs WHERE action_run_id=?", (action_run_id,)).fetchone()

    def action_run_attempts(self, action_run_id: str) -> int:
        row = self.action_run(action_run_id)
        return int(row["attempts"])

    def set_action_run(self, action_run_id: str, state: str, *, output: dict | None = None, error: str | None = None) -> None:
        self.conn.execute("UPDATE action_runs SET state=?,attempts=attempts+?,output_json=COALESCE(?,output_json),error=?,updated_at=? WHERE action_run_id=?", (state, int(state == "running"), json.dumps(output, sort_keys=True) if output is not None else None, error, now(), action_run_id))

    def save_evaluation(self, episode_id: str, grader: str, score: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?)", (episode_id, grader, json.dumps(score, sort_keys=True), now()))

    def campaign_status(self, campaign: str) -> dict:
        row = self.conn.execute("SELECT status FROM campaigns WHERE campaign=?", (campaign,)).fetchone()
        if not row: return {"campaign": campaign, "exists": False}
        counts = self.conn.execute("SELECT status,COUNT(*) n FROM episodes WHERE campaign=? GROUP BY status", (campaign,)).fetchall()
        binding = self.study_binding(campaign)
        configured = json.loads(self.conn.execute("SELECT config_json FROM campaigns WHERE campaign=?", (campaign,)).fetchone()["config_json"])["budget"]["usd"]
        update = self.conn.execute("SELECT MAX(ceiling_usd) ceiling FROM budget_updates WHERE campaign=?", (campaign,)).fetchone()["ceiling"]
        ceiling = max(float(configured), float(update)) if update is not None else float(configured)
        spent = self.campaign_spend(campaign)
        cost = self.conn.execute("""SELECT
            COALESCE(SUM(CASE WHEN r.state='completed' THEN r.actual_usd ELSE 0 END),0) confirmed,
            COALESCE(SUM(CASE WHEN r.state IN ('reserved','submitted','unknown_outcome') THEN r.reserved_usd ELSE 0 END),0) reserved,
            COALESCE(SUM(CASE WHEN r.state='unknown_outcome' THEN 1 ELSE 0 END),0) unknown_requests
            FROM requests r JOIN episodes e ON e.episode_id=r.episode_id WHERE e.campaign=?""", (campaign,)).fetchone()
        return {"campaign": campaign, "exists": True, "status": row["status"], "episodes": {r["status"]: r["n"] for r in counts},
                "accounted_usd": spent, "confirmed_usd": float(cost["confirmed"]), "reserved_or_uncertain_usd": float(cost["reserved"]),
                "unknown_provider_requests": int(cost["unknown_requests"]), "budget_ceiling_usd": ceiling,
                "budget_overrun_usd": max(0, spent-ceiling), "manifest_hash": binding["manifest_hash"] if binding else None}
