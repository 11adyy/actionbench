"""Prove that an already completed smoke episode caused no repeated API calls."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def signature(database: Path) -> dict:
    connection=sqlite3.connect(database)
    rows=connection.execute("SELECT * FROM calls ORDER BY id").fetchall()
    connection.close()
    encoded=json.dumps(rows,separators=(",",":"),ensure_ascii=False).encode()
    return {"count":len(rows),"sha256":hashlib.sha256(encoded).hexdigest()}


def main() -> None:
    mode,db_path,snapshot_path=sys.argv[1:]
    current=signature(Path(db_path))
    snapshot=Path(snapshot_path)
    if mode=="snapshot":
        snapshot.write_text(json.dumps(current)+"\n")
    elif mode=="verify":
        if current!=json.loads(snapshot.read_text()):
            raise ValueError("Resume repeated or changed a provider call")
        print(json.dumps({"resume_preserved_calls":True,**current}))
    else:raise ValueError(mode)


if __name__=="__main__":main()
