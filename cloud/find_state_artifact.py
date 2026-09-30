"""Print the newest unexpired state artifact ID for one exact campaign."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request


def main() -> None:
    campaign = os.environ["AB_CAMPAIGN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    name = re.compile(rf"actionbench-state-{re.escape(campaign)}-[0-9]+-[0-9]+\Z")
    matches = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100&page={page}"
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "User-Agent": "ActionBench-state-restore",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            artifacts = json.load(response)["artifacts"]
        matches.extend(item for item in artifacts if not item["expired"] and name.fullmatch(item["name"]))
        if len(artifacts) < 100:
            break
        page += 1
    if matches:
        latest = max(matches, key=lambda item: (item["created_at"], item["id"]))
        print(latest["id"])


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError) as exc:
        print(f"Cannot locate campaign state artifact: {exc}", file=sys.stderr)
        raise SystemExit(2)
