"""Stable identity for the generated-script runtime across Docker rebuilds."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    image=sys.argv[1]
    dockerfile=Path("docker/skill-v2.Dockerfile").read_bytes()
    runtime=subprocess.run(["docker","run","--rm","--network","none","--read-only",
                            "--cap-drop","ALL","--security-opt","no-new-privileges",image,
                            "python","-c","import sys,subprocess;print(sys.version);print(subprocess.check_output([sys.executable,'-m','pip','freeze','--all'],text=True))"],
                           capture_output=True,text=True,check=True,timeout=60)
    payload=json.dumps({"dockerfile_sha256":hashlib.sha256(dockerfile).hexdigest(),
                        "python_and_distributions":runtime.stdout},sort_keys=True).encode()
    print(hashlib.sha256(payload).hexdigest())


if __name__=="__main__":main()
