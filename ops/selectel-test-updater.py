#!/usr/bin/env python3
"""Deploy only a validated test-01 page to its isolated Selectel directory."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

REPO = "TanemTech/test-01"
API = f"https://api.github.com/repos/{REPO}"
DEST = Path("/var/www/tanem-test-01/index.html")
STATE = Path("/home/tanem-deploy/.local/state/tanem-test-01.json")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def fetch(url, max_size=100000):
    request = urllib.request.Request(url, headers={
        "User-Agent": "tanem-test01-sync/1.0",
        "Accept": "application/vnd.github+json" if url.startswith("https://api.github.com/") else "text/html"
    })
    with urllib.request.urlopen(request, timeout=25) as response:
        value = response.read(max_size + 1)
    if len(value) > max_size:
        raise ValueError("Response too large")
    return value


def run():
    head = json.loads(fetch(f"{API}/branches/main", 200000))["commit"]["sha"]
    if not SHA.fullmatch(head):
        raise ValueError("Invalid commit SHA")
    runs = json.loads(fetch(f"{API}/actions/workflows/validate.yml/runs?branch=main&per_page=15", 300000))["workflow_runs"]
    verified = any(
        r.get("head_sha") == head and
        r.get("event") in ("push", "workflow_dispatch") and
        r.get("status") == "completed" and
        r.get("conclusion") == "success"
        for r in runs
    )
    if not verified:
        print(f"SKIP: GitHub validation not successful for current main {head}")
        return 0
    if STATE.exists():
        try:
            if json.loads(STATE.read_text()).get("sha") == head:
                print(f"UNCHANGED: {head}")
                return 0
        except (ValueError, OSError):
            pass
    page = fetch(f"https://raw.githubusercontent.com/{REPO}/{head}/index.html")
    if b"<!doctype html>" not in page.lower() or b"TANEM" not in page:
        raise ValueError("Unexpected test site contents")
    if not DEST.parent.is_dir():
        raise ValueError("Test document root does not exist")
    if DEST.is_symlink():
        raise ValueError("Refusing to replace symlink")
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tempname = tempfile.mkstemp(prefix=".tanem-verify-", dir=DEST.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(page)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tempname, 0o644)
        os.replace(tempname, DEST)
    finally:
        if os.path.exists(tempname):
            os.unlink(tempname)
    STATE.write_text(json.dumps({"sha": head}) + "\n", encoding="utf8")
    print(f"DEPLOYED: {head} (isolated test-01 site)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
