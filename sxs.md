# Perf Side-by-Side (SxS) - Project Documentation

## Project Overview

Downloads and compares performance test videos from Mozilla CI pushes. Given two revisions (or a perfcompare URL), downloads video artifacts and displays them in a synchronized side-by-side web viewer.

Supports two test types:
- **Browsertime/Raptor** (framework 13) — tp6 pageload tests, annotated `.mp4` videos
- **mozperftest Android startup** (framework 15) — applink, homeview, shopify, tab-restore startup tests; produce `.mp4` + `.png` screenshots

## Repository Structure

```
perf-sxs-cli/
├── perf_sxs.py            # Main CLI — download, filter, organize
├── viewer.py              # Flask viewer
├── analyze.py             # Standalone Claude vision analysis
├── templates/viewer.html  # Jinja2 template — all HTML/CSS/JS
├── static/fuse.min.js     # Fuse.js v7 fuzzy search (offline)
├── .claude/skills/analyze-perf-videos/SKILL.md  # Claude Code skill
├── pyproject.toml
└── README.md
```

## CLI Usage

```bash
# Perfcompare URL (recommended — auto high-confidence filter, both frameworks)
uv run python perf_sxs.py "https://perf.compare/compare-results?baseRev=X&newRev=Y&..."

# Lando URL
uv run python perf_sxs.py "https://perf.compare/compare-lando-results?baseLando=181700&newLando=181701&..."

# Two raw revisions
uv run python perf_sxs.py <base-rev> <new-rev>

# Single revision (no comparison)
uv run python perf_sxs.py <rev> --no-compare

# Key flags
--all-tests          # skip high-confidence filter
--all-runs           # download all runs (default: median only)
--platforms a55      # substring filter on task name
--tests applink,cnn  # substring filter on task name
--max-downloads 20   # concurrency (default 10)
--no-serve           # don't launch viewer
--port 3333          # viewer port
```

**Output dir is wiped on every re-run** (3-second cancellation window). Use `--output ./new_dir` to preserve old downloads.

## Download Flow

1. Parse input → extract base/new revision hashes
   - Handles: perfcompare URL, lando URL (resolves via `api.lando.services.mozilla.com`), Treeherder URL, plain hash
2. Find task groups — `gecko.v2.{repo}.revision.{rev}.taskgraph` index; **all indexed tasks are fetched** since mozilla-central can have multiple task groups per revision (main CI + perf pushes)
3. Fetch tasks from all groups, deduplicate by task ID
4. Fetch high-confidence tests from Treeherder — **both framework 13 and 15 queried in parallel** for full coverage of video-producing test types
5. Filter: `_is_video_task()` accepts browsertime (non-profiling) OR `perftest-*-startup-*`; status=completed; platform/test filters; high-conf filter
6. For each task:
   - Try `public/test_info/browsertime-videos-annotated.tgz` → original → videos (works for browsertime and some perftest)
   - If all 404: list TC artifacts, find non-build archive (e.g. `public/build/<test>.tgz`) or direct mp4/png files
   - Extract archive, group PNGs with their video run via `_group_images_by_video()`
   - Download `perfherder-data.json` → find median run index
   - For median-only: delete non-median videos AND their corresponding images
7. Organize into `base/` and `new/` dirs
8. Write `comparisons.json`
9. Launch Flask viewer

## Task Naming Conventions

**Browsertime:**
```
test-{platform}/{opt|debug}-browsertime-{type}-{browser}-{site}
test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon
```
Parsed by splitting on `-browsertime-`. Platform flattened: `/` → `_`.

**mozperftest startup:**
```
perftest-{platform}/{opt|debug}-{test}
perftest-android-hw-a55-aarch64-shippable/opt-startup-fenix-newssite-applink-startup
```
Also handles flat format (no `/opt`):
```
perftest-android-hw-a55-aarch64-shippable-startup-fenix-newssite-applink-startup
```
Flat format parsed by splitting on last `-shippable-` (or `-opt-`/`-debug-`).

## comparisons.json Schema

```json
{
  "mode": "compare" | "single",
  "base_revision": "abc123...",
  "new_revision": "def456...",
  "comparisons": {
    "platform/test-name": {
      "platform": "perftest-android-hw-a55-aarch64-shippable",
      "test_name": "startup-fenix-newssite-applink-startup",
      "base_videos": ["base/.../video.mp4"],
      "new_videos": ["new/.../video.mp4"],
      "base_median_idx": 2,
      "new_median_idx": 1,
      "same_task_warning": false,
      "base_images": [["base/.../screenshot.png"], ["base/.../screenshot2.png"]],
      "new_images": [["new/.../screenshot.png"], ["new/.../screenshot2.png"]]
    }
  }
}
```

- `base_median_idx`/`new_median_idx` are `null` when `--all-runs` was NOT used (only 1 video)
- `base_images`/`new_images` are **lists-of-lists** — one sublist per video run; empty sublists for browsertime tests with no PNGs
- `same_task_warning: true` → base and new resolved to same task ID; re-download to fix

## Image Grouping Logic

`_group_images_by_video(mp4s, pngs)` → `list[list[Path]]`

- If each mp4 is in its own unique subdirectory: group pngs by matching parent dir
- Otherwise (flat layout, all in same dir): distribute pngs evenly by index (len(pngs) // len(mp4s) per run)
- 1:1 fallback if neither works

For median-only downloads: only the images grouped with the median video are kept; others are deleted from disk.

## Median Run Detection

Uses `perfherder-data.json` artifact (`public/perfherder-data.json`):
- `suites[0].subtests[0].replicates` → list of per-run values
- `suites[0].subtests[0].value` → the median (aggregate)
- Picks replicate index closest to `value`
- Falls back to index 0 on any error

## High-Confidence Filter

When a perfcompare URL is provided (and `--all-tests` not set):
- Queries `https://treeherder.mozilla.org/api/perfcompare/results/` for **both framework 13 and 15** concurrently
- Returns union of `(suite, platform)` pairs with `confidence_text == "High"`
- For browsertime tasks: exact `(suite, platform)` match required
- For perftest tasks: tries exact match first, then fuzzy platform+suite substring match (TC and Treeherder names can differ)

## Viewer

Flask at `http://localhost:3333`, reads `comparisons.json` + `analysis.json` (if present).

**Features:**
- Side-by-side synchronized playback; auto-plays on test select AND run change
- Screenshots panel (collapsible, auto-expands on test select) — shows base/new PNGs for current run; clicking zooms to fullscreen
- Run selector syncs both video source and screenshot panel
- Fuse.js fuzzy sidebar search (offline, extended syntax)
- Collapsible platform sections
- Analysis panel (color-coded regression badges) if `analysis.json` present
- ⚠ badge on same-task comparisons

**Routes:** `/` (template), `/video/<path>` (serves mp4+png), `/api/comparisons`, `/api/analysis`

## Artifact Paths

**Browsertime:**
1. `public/test_info/browsertime-videos-annotated.tgz` (preferred — has timing overlay)
2. `public/test_info/browsertime-videos-original.tgz`
3. `public/test_info/browsertime-videos.tgz`

**mozperftest startup (fallback discovery):**
- Lists TC artifacts via `GET /task/{taskId}/artifacts`
- Finds archives not matching excluded filenames (`target.tar.bz2`, `target.zip`, `target.apk`, `build.tar.gz`) or excluded words (`log`, `mozharness`, `sdk`, `crashreporter`)
- e.g. `public/build/newssite-applink-startup.tgz` — contains `.mp4` + `.png` per iteration
- Falls back to direct `.mp4`/`.png` artifact files if no archive found

## Output Directory Structure

```
sxs_videos/
├── comparisons.json
├── analysis.json          (if analyzed)
├── analysis_report.html   (if analyzed)
├── base/
│   └── <platform>/
│       └── <test-name>/
│           └── <task-id>/
│               └── <nested paths from tgz>
│                   └── *.mp4, *.png
└── new/
    └── (same structure)
```

## Known Patterns / Gotchas

- **Multiple task groups per revision**: mozilla-central often has 14+ task groups (main CI + perf pushes). The tool fetches all of them. Try pushes have 1.
- **applink tests not in main CI group**: `startup-fenix-*` tests run in separate perf task groups, not the main 5000-task CI group. The multi-group fetch handles this automatically.
- **Annotated videos have timing overlays**: DOMContentLoaded, FirstVisualChange, SpeedIndex, etc. No need to surface numbers in viewer — they're in the video.
- **PSNR/SSIM for annotated videos**: PSNR 12–20 dB with SSIM=1.0 is normal — timing text differs between runs. Only SSIM < 0.95 is a real visual regression.
- **same_task_warning**: base and new resolved to the same task ID. Re-download with current versions.
- **Platform names**: `/` replaced with `_` (e.g., `test-linux1804-64-qr/opt` → `test-linux1804-64-qr_opt`)
- **Pasted URLs with newlines**: sanitized automatically (stray `\n`/`\r` stripped before parsing)
- **`--all-runs` writes `median_idx.txt`**: sidecar in extract dir so viewer can label median run

## Video Analysis

### Claude Code skill (primary)
```
/analyze-perf-videos
/analyze-perf-videos ./sxs_videos amazon,applink
```
Defined in `.claude/skills/analyze-perf-videos/SKILL.md` with `context: fork`. Ships in repo. Runs as isolated subagent.

### analyze.py (standalone)
```bash
uv sync --extra analyze
uv run python analyze.py ./sxs_videos --concurrency 10
```
Requires `ANTHROPIC_API_KEY` + `ffmpeg`.

### analysis.json schema
```json
{
  "comparisons": {
    "platform/test": {
      "summary": "...",
      "regression": true | false | null,
      "severity": "none" | "low" | "medium" | "high",
      "psnr": 38.5,
      "ssim": 0.973,
      "observations": ["..."]
    }
  }
}
```

## Development

```bash
uv sync --extra dev
uv run pre-commit install
uv run ruff check .
uv run ruff format .
uv run pytest tests/ -v
```

Pre-commit hooks: ruff format + lint, mypy, pytest.
