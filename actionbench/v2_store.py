"""Small durable ledger for the Deep Agents study; old campaigns remain untouched."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path, campaign: str, config: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deep Agents executes tools in worker threads; the coordinator remains
        # single-process, and SQLite still serializes write transactions.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS campaign(id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, config_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS packages(
          family TEXT NOT NULL, replica INTEGER NOT NULL, condition TEXT NOT NULL,
          status TEXT NOT NULL, path TEXT, sha256 TEXT, error TEXT,
          PRIMARY KEY(family,replica,condition));
        CREATE TABLE IF NOT EXISTS episodes(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, family TEXT NOT NULL, replica INTEGER NOT NULL,
          condition TEXT NOT NULL, budget_usd REAL NOT NULL, status TEXT NOT NULL,
          score REAL, grader_json TEXT, answer TEXT, error TEXT, duration_seconds REAL);
        CREATE TABLE IF NOT EXISTS calls(
          id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, step TEXT NOT NULL,
          state TEXT NOT NULL, reserved_usd REAL NOT NULL, actual_usd REAL,
          input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER,
          provider_id TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS invocations(
          episode_id TEXT NOT NULL, ordinal INTEGER NOT NULL, state TEXT NOT NULL,
          input_hash TEXT NOT NULL, output_json TEXT, error TEXT,
          PRIMARY KEY(episode_id,ordinal));
        """)
        row = self.db.execute("SELECT config_hash FROM campaign WHERE id=?", (campaign,)).fetchone()
        fingerprint = digest(config)
        if row and row["config_hash"] != fingerprint:
            raise ValueError("Frozen campaign configuration changed")
        self.db.execute("INSERT OR IGNORE INTO campaign VALUES(?,?,?)", (campaign, fingerprint, canonical(config)))

    def close(self):
        self.db.close()

    def package(self, family: str, replica: int, condition: str):
        return self.db.execute("SELECT * FROM packages WHERE family=? AND replica=? AND condition=?", (family, replica, condition)).fetchone()

    def save_package(self, family: str, replica: int, condition: str, status: str, path: str | None, sha256: str | None, error: str | None = None):
        self.db.execute("INSERT OR REPLACE INTO packages VALUES(?,?,?,?,?,?,?)", (family, replica, condition, status, path, sha256, error))

    def episode(self, episode_id: str):
        return self.db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()

    def begin_episode(self, episode_id: str, task_id: str, family: str, replica: int, condition: str, budget: float):
        self.db.execute("INSERT OR IGNORE INTO episodes(id,task_id,family,replica,condition,budget_usd,status) VALUES(?,?,?,?,?,?,'queued')", (episode_id,task_id,family,replica,condition,budget))
        row = self.episode(episode_id)
        if (row["task_id"],row["family"],row["replica"],row["condition"],row["budget_usd"]) != (task_id,family,replica,condition,budget):
            raise ValueError("Episode identity or budget changed on resume")
        return row

    def set_episode(self, episode_id: str, status: str, *, score=None, grader=None, answer=None, error=None, duration=None):
        self.db.execute("UPDATE episodes SET status=?,score=?,grader_json=?,answer=?,error=?,duration_seconds=? WHERE id=?", (status,score,canonical(grader) if grader is not None else None,answer,error,duration,episode_id))

    def spent(self, episode_id: str | None = None) -> float:
        where = "WHERE episode_id=?" if episode_id else ""
        args = (episode_id,) if episode_id else ()
        return float(self.db.execute(f"SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) FROM calls {where}",args).fetchone()[0])

    def unresolved(self, episode_id: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM calls WHERE episode_id=? AND state='submitted' LIMIT 1",(episode_id,)).fetchone())

    def reserve(self, call_id: str, episode_id: str, step: str, usd: float, episode_limit: float, campaign_limit: float):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM calls WHERE id=?",(call_id,)).fetchone():
                raise ValueError("Duplicate model call; inspect the saved outcome before retrying")
            if self.spent(episode_id)+usd > episode_limit+1e-12 or self.spent()+usd > campaign_limit+1e-12:
                raise ValueError("Model budget exhausted")
            self.db.execute("INSERT INTO calls(id,episode_id,step,state,reserved_usd) VALUES(?,?,?,'submitted',?)",(call_id,episode_id,step,usd))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def complete(self, call_id: str, *, input_tokens: int, cached_tokens: int, output_tokens: int, actual_usd: float, provider_id: str | None):
        self.db.execute("UPDATE calls SET state='completed',actual_usd=?,input_tokens=?,cached_tokens=?,output_tokens=?,provider_id=? WHERE id=?",(actual_usd,input_tokens,cached_tokens,output_tokens,provider_id,call_id))

    def reject(self, call_id: str, error: str):
        self.db.execute("UPDATE calls SET state='rejected',actual_usd=0,error=? WHERE id=?",(error[:500],call_id))

    def invocation(self, episode_id: str, ordinal: int):
        return self.db.execute("SELECT * FROM invocations WHERE episode_id=? AND ordinal=?",(episode_id,ordinal)).fetchone()

    def start_invocation(self, episode_id: str, ordinal: int, input_hash: str):
        row=self.invocation(episode_id,ordinal)
        if row:
            if row["input_hash"]!=input_hash: raise ValueError("Invocation input changed on resume")
            return row
        self.db.execute("INSERT INTO invocations VALUES(?,?,'submitted',?,NULL,NULL)",(episode_id,ordinal,input_hash))
        return None

    def finish_invocation(self, episode_id: str, ordinal: int, output: dict):
        self.db.execute("UPDATE invocations SET state='completed',output_json=? WHERE episode_id=? AND ordinal=?",(canonical(output),episode_id,ordinal))
