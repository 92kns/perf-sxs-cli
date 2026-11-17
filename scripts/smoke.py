"""Live smoke test against real Taskcluster/Treeherder data (run nightly in CI).

Finds a recent autoland push that ran a small browsertime test, downloads
it with perf-sxs, and runs perf-sxs-analyze prepare on the result. Fails if the
CI artifact layout or Treeherder API shape the code depends on has changed
(see docs/internals.md, "CI facts the code relies on").

    uv run python scripts/smoke.py [--pushes 80] [--tests ...] [--platforms ...]

Perf tests run on roughly every 20th autoland push, and desktop tp6 page loads
don't run on mozilla-central at all (only their profiling variant), hence the
defaults.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from perf_sxs.api import find_task_group_ids, get_tasks_for_revision, new_session

PUSHES_API = "https://treeherder.mozilla.org/api/project/{repo}/push/?count={count}"


def recent_revisions(repo: str, count: int) -> list[str]:
    req = urllib.request.Request(
        PUSHES_API.format(repo=repo, count=count), headers={"User-Agent": "perf-sxs-smoke"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return [p["revision"] for p in json.load(resp)["results"]]


async def find_push(repo: str, revisions: list[str], test: str, platform: str) -> str | None:
    """First revision with a completed task matching both filters."""
    async with new_session() as session:
        for rev in revisions:
            try:
                groups = await find_task_group_ids(session, rev, repo)
            except Exception:
                continue  # not indexed yet, or a push without tasks
            for task in await get_tasks_for_revision(session, groups):
                name = task["task"]["metadata"]["name"]
                if test in name and platform in name and task["status"]["state"] == "completed":
                    return rev
            print(f"{rev[:12]}: no completed {test} task")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="autoland")
    parser.add_argument("--pushes", type=int, default=80)
    parser.add_argument("--tests", default="tp6-essential-firefox-amazon")
    parser.add_argument("--platforms", default="linux2404-64-shippable/")
    args = parser.parse_args()

    revs = recent_revisions(args.repo, args.pushes)
    found = asyncio.run(find_push(args.repo, revs, args.tests, args.platforms))
    for rev in [found] if found else []:
        out = Path(tempfile.mkdtemp(prefix="perf-sxs-smoke-"))
        url = f"https://treeherder.mozilla.org/jobs?repo={args.repo}&revision={rev}"
        proc = subprocess.run(
            ["perf-sxs", url, "--no-compare", "--tests", args.tests, "--platforms",
             args.platforms, "--json", "--no-serve", "-o", str(out / "v")],
            capture_output=True, text=True,
        )  # fmt: skip
        summary = json.loads(proc.stdout or "{}")
        if proc.returncode != 0:
            error = summary.get("error", proc.stderr[-500:])
            print(f"{rev[:12]}: perf-sxs failed: {error}")
            return 1

        print(f"{rev[:12]}: {json.dumps(summary)}")
        if not summary["downloaded"]["base"]["videos"] or summary["missing"]:
            print("FAIL: tasks found but no videos downloaded (artifact layout changed?)")
            return 1
        meta = json.loads(Path(summary["metadata_path"]).read_text())
        comp = next(iter(meta["comparisons"].values()))
        if not comp["base_videos"][0].endswith(".mp4"):
            print(f"FAIL: unexpected video path {comp['base_videos'][0]}")
            return 1
        if not list((out / "v").rglob("perfherder-data.json")):
            print("FAIL: no perfherder-data.json saved (artifact path changed?)")
            return 1

        prep = subprocess.run(["perf-sxs-analyze", "prepare", str(out / "v")])
        manifest = json.loads((out / "v/analysis/manifest.json").read_text())
        entry = next(iter(manifest["comparisons"].values()))
        if prep.returncode != 0 or entry.get("error") or not entry.get("sheet"):
            print(f"FAIL: perf-sxs-analyze prepare: {entry.get('error')}")
            return 1
        bt = entry["sides"]["base"].get("browsertime")
        if not bt:
            print("FAIL: no browsertime metrics found in perfherder data")
            return 1
        print(f"OK: {entry['key']} browsertime={json.dumps(bt)}")
        return 0

    print(f"FAIL: none of the last {args.pushes} {args.repo} pushes ran {args.tests}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
