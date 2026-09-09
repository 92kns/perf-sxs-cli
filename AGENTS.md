# perf-sxs-cli — Agent Usage Guide

`perf-sxs` downloads browsertime/mozperftest videos from one or two Mozilla
Try (or mozilla-central) pushes and organizes them for side-by-side
comparison. `perf-sxs-viewer` serves a directory `perf-sxs` already
downloaded, for interactive browsing later.

## Installation

```bash
uv sync
# or, to get plain `perf-sxs` / `perf-sxs-viewer` commands on PATH:
uv tool install .
```

## perf-sxs

### Input modes

```bash
# perfcompare URL (recommended) — auto-fetches high-confidence tests from
# Treeherder (frameworks 13 + 15) and downloads only those videos
perf-sxs "https://perf.compare/compare-results?baseRev=...&newRev=..."

# lando perfcompare URL — IDs resolved to revision hashes via the Lando API
perf-sxs "https://perf.compare/compare-lando-results?baseLando=181700&newLando=181701"

# two revisions or Treeherder URLs
perf-sxs 881d2bbfaf53 56290454af18

# single revision, no comparison target
perf-sxs 881d2bbfaf53 --no-compare
```

Other flags an agent is likely to need: `--platforms`/`-p` and `--tests`/`-t`
(comma-separated filters), `--all-tests` (skip the High-confidence filter),
`--all-runs` (default is median-run-only), `--confidence-json <path>` (use a
local perfcompare JSON instead of hitting the Treeherder API), `--output`/`-o`,
`--no-serve` (skip auto-launching the viewer).

### --json mode

Pass `--json` and every progress/diagnostic line that would normally print to
stdout is routed to stderr instead, so stdout carries exactly one JSON object.

```bash
perf-sxs --json --no-serve "https://perf.compare/compare-results?baseRev=881d2bbf...&newRev=56290454..."
```

On success:

```json
{
  "mode": "compare",
  "base_revision": "881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8",
  "base_repo": "try",
  "new_revision": "56290454af1890c3344757213fc7199839fe3e7f",
  "new_repo": "try",
  "platforms": null,
  "tests": null,
  "output_dir": "sxs_videos",
  "video_tasks_found": 12,
  "downloaded": {
    "base": {"videos": 6, "images": 0},
    "new": {"videos": 6, "images": 0}
  },
  "missing": [],
  "comparisons_count": 6,
  "metadata_path": "sxs_videos/comparisons.json",
  "viewer": {"serving": false, "command": "perf-sxs-viewer sxs_videos"}
}
```

`mode` is `"single"` when run with `--no-compare` (then `new_revision`/
`new_repo` are `null`). `platforms`/`tests` echo the `--platforms`/`--tests`
filters actually applied (`null` if not passed). `missing` lists
`{label, platform, test_name}` for any task where no video artifact could be
found. If `--no-serve` wasn't passed, `viewer` is
`{"serving": true, "url": "http://host:port"}` instead, and — because the
JSON is printed *before* the Flask server starts blocking — a caller reading
stdout still gets the summary even though the process doesn't exit. No
browser tab is auto-launched in `--json` mode; that's a human convenience,
not something a script wants.

On any failure (bad argument count, an unparseable single-argument URL, a
Lando resolution failure, a non-empty `--output` dir without `--force`, or no
matching video tasks found), stdout gets one error object and the process
exits 1:

```json
{"error": "No matching video tasks found", "exit_code": 1}
```

This mirrors the `{"error": ..., "exit_code": 1}` shape `perftest-brain` uses
elsewhere in this project. Without `--json`, all of the above prints the
original multi-line prose to stdout, unchanged.

### What perf-sxs refuses to do (and why)

- **Won't overwrite a non-empty `--output` directory** unless `--force` is
  passed — pass `--force` to wipe it, or pick a different `--output` path.
  This is a deliberate gate against accidentally clobbering a previous run's
  videos; there's no silent merge behavior.
- **Won't extract an archive member outside the target directory.** CI
  artifacts (`.tgz`/`.zip` files from Taskcluster) are treated as untrusted
  input: a member name containing `../` or an absolute path, or a
  symlink/hardlink/device member, is rejected outright
  (`UnsafeArchiveMemberError`) rather than silently skipped — a malicious or
  corrupted archive fails the whole extraction loudly instead of partially
  succeeding or writing outside the output directory ("zip slip"/"tar slip").

## perf-sxs-viewer

Serves a directory `perf-sxs` already populated (reads `comparisons.json`,
and `analysis.json` if present from `analyze.py`).

```bash
perf-sxs-viewer ./sxs_videos
perf-sxs-viewer ./sxs_videos --json   # {"serving": true, "url": "..."} then blocks
```

`--json` prints `{"serving": true, "url": "http://host:port"}` to stdout
*before* `app.run()` blocks, and skips the auto-launched browser tab. On a
missing directory it prints `{"error": "...", "exit_code": 1}` and exits 1
instead of the prose error. This tool otherwise has no reason for a fuller
`--json` mode: it's a long-running Flask server for interactive human
browsing, not something that produces a one-shot structured result — its own
`/api/comparisons` and `/api/analysis` endpoints are already the
machine-readable surface for whatever's being viewed.

Both `perf-sxs` and `perf-sxs-viewer` default to binding `127.0.0.1`;
`0.0.0.0` (LAN-visible) is opt-in via `--host`.

## Typical agent workflow

1. Get a perfcompare URL — from a PerfCompare comparison, a Lando perfcompare
   link, or two revisions/Treeherder URLs.
2. `perf-sxs --json --no-serve "<url>"` — downloads videos without blocking
   on the viewer's HTTP server, and gives back one JSON object to parse.
3. Check the JSON: `comparisons_count` for how many test/platform pairs came
   back, `missing` for anything that failed to download, `metadata_path` for
   where `comparisons.json` landed.
4. To actually look at the videos: drop `--no-serve` (auto-launches a browser
   tab, human mode only) or run `perf-sxs-viewer <output_dir>` later.
