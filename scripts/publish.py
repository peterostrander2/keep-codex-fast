#!/usr/bin/env python3
"""Publish this fork and verify existing CI, with one dispatch fallback."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "peterostrander2/keep-codex-fast"


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, cwd=ROOT, timeout=60).strip()


def api(path: str, *, dispatch: bool = False):
    args = ["gh", "api", path]
    if dispatch:
        args += ["--method", "POST", "-f", "ref=main"]
    output = command(*args)
    return json.loads(output) if output else None


def save(path: Path, state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def verify_ci(sha: str, state: dict, persist, *, request=api, sleep=time.sleep,
              clock=time.monotonic, timeout: int = 300) -> dict:
    """Reuse exact-SHA runs; persist dispatch intent before making the request."""
    endpoint = f"repos/{REPOSITORY}"
    deadline = clock() + timeout
    observations = 0
    while clock() < deadline:
        remote = request(f"{endpoint}/git/ref/heads/main")["object"]["sha"]
        if remote != sha:
            raise RuntimeError("Remote main moved; refusing to dispatch another commit")
        runs = request(f"{endpoint}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100")["workflow_runs"]
        runs = [run for run in runs if run["head_sha"] == sha]
        if runs:
            run = max(runs, key=lambda row: row["id"])
            state.update(current_step="waiting_for_ci", run_id=run["id"], ci_url=run["html_url"])
            persist()
            if run["status"] == "completed":
                if run["conclusion"] != "success":
                    raise RuntimeError(f"CI concluded {run['conclusion']}")
                return run
        elif observations >= 1 and not state.get("dispatch_attempted"):
            state.update(current_step="dispatching_existing_ci", dispatch_attempted=True)
            persist()
            request(f"{endpoint}/actions/workflows/ci.yml/dispatches", dispatch=True)
        observations += 1
        sleep(min(10, max(0, deadline - clock())))
    raise TimeoutError("No successful exact-SHA CI result within the bounded wait")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path,
                        help="External state file; also writes a sibling terminal receipt")
    args = parser.parse_args()
    receipt = args.receipt.expanduser().resolve()
    if receipt.is_relative_to(ROOT):
        parser.error("receipt must be outside the repository")
    if command("git", "-C", str(ROOT), "status", "--porcelain"):
        parser.error("publish requires a clean checkout")
    if command("git", "-C", str(ROOT), "branch", "--show-current") != "main":
        parser.error("publish requires the canonical main branch")
    sha = command("git", "-C", str(ROOT), "rev-parse", "HEAD")
    prior = json.loads(receipt.read_text()) if receipt.exists() else {}
    state = {"schema": "keep_codex_fast_publish_v1", "sha": sha,
             "repository": REPOSITORY, "status": "RUNNING", "current_step": "push",
             "dispatch_attempted": prior.get("sha") == sha and prior.get("dispatch_attempted", False)}
    persist = lambda: save(receipt, state)
    persist()
    try:
        command("git", "-C", str(ROOT), "push", f"https://github.com/{REPOSITORY}.git", f"{sha}:refs/heads/main")
        state.update(current_step="locating_ci", last_completed_step="push")
        persist()
        run = verify_ci(sha, state, persist)
        state.update(status="PASS", current_step="complete", last_completed_step="ci",
                     conclusion=run["conclusion"])
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, RuntimeError, TimeoutError) as exc:
        state.update(status="FAILED", error=type(exc).__name__ + ": " + str(exc))
    persist()
    save(receipt.with_suffix(".terminal.json"), state.copy())
    print(json.dumps(state, indent=2))
    return 0 if state["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
