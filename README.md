# Perf Side-by-Side CLI Tool

Async Python tool for downloading and comparing performance test videos from Mozilla CI. Supports **browsertime/raptor** (tp6 pageload, multi-step tests like nav-bench) and **mozperftest Android startup** (applink, homeview, shopify, etc.) tests, comparing two pushes or two browsers.

## Installation

```bash
uv sync
```

## Usage

### Perfcompare URL (recommended)

```bash
uv run perf-sxs "https://perf.compare/compare-results?baseRev=BASE&newRev=NEW&..."
```

The tool automatically fetches high-confidence tests from Treeherder (frameworks 13 + 15 in parallel) and downloads only those videos. Works for both browsertime and mozperftest comparisons, including mixed perfcompare links.

```bash
# Download all tests, ignore confidence filter
uv run perf-sxs "https://perf.compare/..." --all-tests

# Download all tests but narrow to specific ones
uv run perf-sxs "https://perf.compare/..." --all-tests --tests applink

# Download all runs per test (default: median only)
uv run perf-sxs "https://perf.compare/..." --all-runs
```

### Mann-Whitney U significance

perfcompare can compare with either Student's t-test or Mann-Whitney U. The
filter follows the URL's `test_version` (default `student-t`); override it with
`--test-version`:

```bash
uv run perf-sxs "https://perf.compare/compare-results?..." --test-version mann-whitney-u
```

- `student-t`: rows with `confidence_text == "High"`
- `mann-whitney-u`: rows Treeherder marks `direction_of_change` = `regression`/`improvement`
  (significant p-value **and** non-negligible Cliff's delta)

If Treeherder answers but nothing is significant, the tool exits with an error
instead of downloading everything — pass `--all-tests` for that.

### Cross-browser (e.g. Firefox vs Chrome)

```bash
# Same push: base = firefox tasks, new = chrome tasks
uv run perf-sxs 881d2bbfaf53 --cross-browser firefox,chrome

# Two pushes: base push's fenix tasks vs new push's chrome-m tasks (e.g. applink)
uv run perf-sxs "https://treeherder.mozilla.org/jobs?repo=mozilla-central&revision=AAA" \
                "https://treeherder.mozilla.org/jobs?repo=mozilla-central&revision=BBB" \
                --cross-browser fenix,chrome-m --tests applink --platforms a55
```

App names are the task-name tokens raptor uses: `firefox`, `chrome`, `safari`,
`safari-tp`, `custom-car` (desktop) and `fenix`, `geckoview`, `refbrow`,
`chrome-m`, `cstm-car-m` (Android). Tests are paired by platform and by test
name with the app swapped out (shown as e.g. `…-firefox-vs-chrome-…`); tests
that only ran for one browser are skipped before download. The significance
filter is off in this mode. On mozilla-central the Chrome and Firefox variants
of a test often run in different cron pushes — use the two-revision form then.

### Lando URL

```bash
uv run perf-sxs \
    "https://perf.compare/compare-lando-results?baseLando=181700&newLando=181701&baseRepo=try&newRepo=try&framework=13"
```

Lando IDs are resolved to revision hashes via the Lando API. If the job is still pending, wait ~30s and retry.

### Two revisions

```bash
uv run perf-sxs \
    881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8 \
    56290454af1890c3344757213fc7199839fe3e7f
```

### Single revision (no comparison)

```bash
uv run perf-sxs 881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8 --no-compare
```

Shows a single full-width video panel instead of side-by-side.

### View previously downloaded videos

```bash
uv run perf-sxs-viewer ./sxs_videos
```

Both `perf-sxs` and `perf-sxs-viewer` are also available as regular commands (no
`uv run` prefix needed) after `uv tool install .` or `pip install .`.

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--platforms`, `-p` | Comma-separated platform filters (e.g., `linux,a55`) | All |
| `--tests`, `-t` | Comma-separated test name filters (e.g., `amazon,applink`) | All |
| `--output`, `-o` | Output directory | `./sxs_videos` |
| `--force` | Overwrite an existing, non-empty output directory | false |
| `--max-downloads`, `-m` | Concurrent downloads | 10 |
| `--no-serve` | Skip auto-launching viewer | false |
| `--all-tests` | Skip High confidence filter | false |
| `--all-runs` | Download all runs (default: median only) | false |
| `--no-compare` | Single revision mode | false |
| `--confidence-json` | Local perfcompare JSON (UI export or raw Treeherder API rows) for the significance filter | None |
| `--test-version` | `student-t` or `mann-whitney-u` significance | URL's, else `student-t` |
| `--cross-browser` | `BASE_APP,NEW_APP`, e.g. `firefox,chrome` | off |
| `--json` | One JSON summary on stdout, progress on stderr (see `AGENTS.md`) | false |
| `--port` | Viewer port | 3333 |
| `--host` | Viewer bind address (use `0.0.0.0` to expose on your LAN) | `127.0.0.1` |

If `--output` already points at a non-empty directory, the tool refuses to run
unless `--force` is passed — pass `--force` to overwrite it, or pick a
different `--output` path.

## How It Works

1. **Parse input** — perfcompare URL, lando URL (resolved via Lando API), Treeherder URL, or plain revision hash
2. **Find task groups** — queries TC index; mozilla-central can have multiple task groups per revision (CI + perf pushes), all are fetched and merged
3. **Fetch significant tests** — queries Treeherder frameworks 13 (browsertime) and 15 (mozperftest) in parallel, using the t-test or MWU (`--test-version`); union of results
4. **Filter tasks** — completed browsertime or perftest-startup tasks, deduplicated by test/platform
5. **Download artifacts** — tries `public/test_info/browsertime-videos-annotated.tgz` first; for perftest tasks falls back to TC artifact listing to find the archive (e.g. `public/build/<test>.tgz`)
6. **Median selection** — downloads `public/test_info/perfherder-data.json` per task and keeps the run closest to the median (per step, for multi-step tests); deletes others unless `--all-runs`
7. **Extract & organize** — extracts archives, groups screenshots with their video run, organizes into `base/` and `new/`
8. **Generate metadata** — writes `comparisons.json`
9. **Launch viewer** — auto-opens browser

## Viewer Features

- **Side-by-side synchronized playback** — auto-plays on test selection and run change
- **Screenshots panel** — collapsible panel below videos showing PNG screenshots from the same run (Android startup tests); click any image to fullscreen
- **Run selector** — switches both video and screenshots together; median labeled by default
- **Sidebar search** — Fuse.js fuzzy search with extended syntax (`'exact`, `^prefix`, `!negate`, `a | b`)
- **Speed control** — 0.25x–2x, global across tests and remembered across reloads
- **Loop toggle** — loop both videos; also global and remembered
- **Multi-step tests** — tests that record several steps in one task (e.g. nav-bench) get one entry per step (`test › step`)
- **Sync toggle** — disable synchronized playback
- **Analysis panel** — appears if `analysis.json` exists (from `/analyze-perf-videos` skill or `analyze.py`)

## Output Structure

```
sxs_videos/
├── comparisons.json
├── base/
│   └── <platform>/
│       └── <test-name>/
│           └── <task-id>/
│               └── *.mp4 (+ *.png for startup tests)
└── new/
    └── (same structure)
```

`comparisons.json` schema:
```json
{
  "mode": "compare",
  "base_revision": "abc123...",
  "new_revision": "def456...",
  "comparisons": {
    "platform/test-name": {
      "step": null,
      "base_videos": ["base/.../1.mp4"],
      "new_videos": ["new/.../1.mp4"],
      "base_median_idx": 2,
      "new_median_idx": 1,
      "base_images": [["base/.../screenshot.png"]],
      "new_images": [["new/.../screenshot.png"]],
      "same_task_warning": false
    }
  }
}
```

`base_images` / `new_images` are lists-of-lists — one sublist per video run, so screenshots stay in sync with the run selector.
Multi-step tests use keys `platform/test-name::step` with `step` set. Top-level
`cross_browser` (`{"base_app", "new_app"}` or `null`) and `test_version` are also recorded.

## Video Analysis

```
/analyze-perf-videos
/analyze-perf-videos ./sxs_videos amazon,cnn
```

Or standalone (requires `ffmpeg` + `ANTHROPIC_API_KEY`):

```bash
uv sync --extra analyze
uv run python analyze.py ./sxs_videos --concurrency 10
```

Writes `analysis.json` (viewer reads it) and `analysis_report.html` (self-contained, shareable).

## Troubleshooting

**0 video tasks found for mozilla-central URL**
- The specific tests may not run on every push — applink/startup tests run in separate perf task groups; the tool now fetches all task groups automatically
- Use `--all-tests --tests <name>` if the confidence filter is excluding what you want

**"No significant changes found"**
- Treeherder answered but nothing passed the filter — try `--test-version mann-whitney-u` (or `student-t`), or `--all-tests`

**Cross-browser finds no pairs**
- App tokens must match task names exactly (`chrome-m`, not `chrome`, on Android)
- Benchmarks (speedometer3, jetstream3, …) pair up but don't record videos

**Viewer shows no comparisons**
- Both base and new need matching platform/test combinations
- Check `comparisons.json` — `comparisons: {}` means download succeeded but files weren't paired

**⚠ warning on a comparison**
- Base and new resolved to the same task ID — re-download to fix

## Development

```bash
uv sync --extra dev
uv run pre-commit install
uv run ruff check .
uv run ruff format .
uv run pytest tests/ -v
```
