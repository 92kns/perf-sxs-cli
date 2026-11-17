# perf-sxs internals

How the code works, and the facts about CI data that it depends on. For
*using* the CLI see `AGENTS.md` / `README.md`. Verified against real
Taskcluster/Treeherder data in Nov 2026. If CI changes, re-check here first.

## Module map (`perf_sxs/`)

| Module | Owns |
|---|---|
| `urls.py` | Pure parsing of perfcompare / lando / Treeherder URLs and bare revisions |
| `api.py` | All network calls (Taskcluster index + queue, Treeherder perfcompare, Lando); `request_with_retry` sets the timeout and retry policy |
| `selection.py` | Task-name parsing, video-task detection, significance predicate (t-test / MWU), cross-browser app matching, median-run choice, `natural_sort_key` |
| `download.py` | Concurrent artifact download, safe tar/zip extraction, per-step median pruning |
| `organize.py` | Step grouping, screenshot↔run grouping, building `comparisons.json` |
| `cli.py` | `perf-sxs`: wiring, `--json` contract, `fail()` error exits |
| `viewer.py` + `templates/viewer.html` | `perf-sxs-viewer` Flask app; all viewer JS/CSS is in the template |
| `analyze.py` (repo root) | Optional Claude-vision analysis → `analysis.json` |

`perf_sxs/__init__.py` re-exports the old flat-module names. Keep it working.

## Pipeline

1. Parse input → base/new `TryPush`. A single non-perfcompare revision is only
   allowed with `--no-compare` or `--cross-browser` (same push on both sides).
2. Task groups: list the `gecko.v2.<repo>.revision.<rev>.taskgraph` index
   (paginated), then look up each indexed task's `taskGroupId` concurrently.
   mozilla-central revisions have many groups (CI and each perf cron).
3. Optional significance filter → set of `(suite, platform)`.
4. `filter_video_tasks`: completed, `-browsertime-` (non-profiling) or
   `perftest-*-startup-*`, then platform/test substring filters, deduped.
5. `build_video_tasks` → `VideoTask`s (cross-browser renames test names and drops unpaired tests).
6. Download, prune to the median, then organize into `base|new/<platform>/<test>/<taskId>/…`.

## CI facts the code relies on

- **perfherder data** is at `public/test_info/perfherder-data.json`.
  `public/perfherder-data.json` is a 404 on current tasks; it's kept only as a
  fallback. Perftest tasks put it under `public/build/perfherder-data-*.json`
  and don't use median pruning.
- **Run videos are `1.mp4 … N.mp4`** (perftest: `vidN_<app>.mp4`). Always sort
  with `natural_sort_key`. Lexical sort puts `10.mp4` second, so the median
  index would point at the wrong video. Replicate `i` ↔ the i-th video in natural order.
- **Browsertime layout:** `browsertime-videos-annotated/<test>/pages/<site>/<path…>/<step>/data/video/N.mp4`.
  Only this `data/video` layout is split into steps.
- **Multi-step tasks (nav-bench):** one task, several `<step>` dirs
  (`duckduckgo-nav-load`, `bbc-nav-subnav`, …). Perfherder has suite
  `nav-bench-overall` with subtests `<step>-score`, plus suites
  `nav-bench.<step>` with `<step>-min-si`, whose `value` is the **min**. The
  step median therefore uses `statistics.median(replicates)`, not `value`.
  Treeherder reports these as suite `nav-bench.<step>`, so the significance
  match also accepts `<suite>.<anything>`. Source: `testing/raptor/browsertime/support-scripts/nav_bench.py`.
- **Perftest profiling variants** put runs in `simpleperf/`, `geckoprofiler/`,
  `simpleperf-geckoprofiler/` subdirs of one task. These stay one group (every
  video is a "run"). Don't split them into steps, or they won't pair with
  non-profiling tasks.
- **App tokens** in task names come from `testing/raptor/raptor/cmdline.py`:
  `firefox chrome safari safari-tp custom-car geckoview refbrow fenix chrome-m cstm-car-m`.
  `find_app_span` matches whole dash-delimited tokens and rejects a prefix of
  a longer known app (`chrome` ≠ `chrome-m`). The token can sit in the middle
  (`benchmark-chrome-speedometer3`) or at the end (`speedometer3-mobile-chrome-m`).
- **Benchmarks** (speedometer3, jetstream3, motionmark…) have no video artifacts.
- **Treeherder perfcompare API** (`/api/perfcompare/results/`):
  `test_version=student-t` rows have `confidence_text`. `mann-whitney-u` rows
  don't. Use `direction_of_change ∈ {regression, improvement}`, which
  Treeherder sets only when the p-value ≤ 0.05 *and* |Cliff's δ| ≥ 0.15
  (`treeherder/perf/stats.py`). `fetch_perfcompare_data_from_treeherder` returns
  `None` when every request fails, and an empty set when it got answers but nothing was significant.
- **Try index expiry:** old try pushes drop out of the TC index ("No tasks found …"), so they can't be re-downloaded.

## comparisons.json

```json
{
  "mode": "compare | single",
  "base_revision": "…", "new_revision": "… | null",
  "cross_browser": {"base_app": "firefox", "new_app": "chrome"} ,
  "test_version": "student-t | mann-whitney-u | null",
  "comparisons": {
    "<platform>/<test>[::<step>]": {
      "platform": "…", "test_name": "…", "step": "… | null",
      "base_videos": ["…"], "new_videos": ["…"],
      "base_median_idx": 2, "new_median_idx": null,
      "base_images": [["…png"]], "new_images": [["…png"]],
      "same_task_warning": false
    }
  }
}
```

`*_median_idx` is set only with `--all-runs`, from the sidecars `median_idx.txt`
(single-step) or `median_idx.json` (`{step: idx}`). `*_images` holds one list per
run. Keys can contain `/` and `::`. In viewer JS, always build DOM ids with
`safeId()`, which replaces every `/` the way Jinja's `replace` does.

## Viewer

Playback prefs (autoplay, loop, speed) are global and stored in localStorage
(`perf-sxs-viewer-prefs`). Changing a `<video>`'s `src` resets `playbackRate`,
so `applyPlaybackPrefs()` sets `defaultPlaybackRate` too. It runs on every
test or run change. Selecting a test pauses the previous one.

## Dev loop

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check . && uv run mypy perf_sxs && uv run pytest -q
```

Tests never hit the network. `tests/test_features.py` stubs `download_artifact`
and `fetch_perfherder_data` in `perf_sxs.download` to test median pruning end to end.
For a live smoke test, use a mozilla-central/autoland Treeherder URL (try
pushes expire), plus `--tests`/`--platforms` to keep it small and `--json --no-serve`.
