# Perf Side-by-Side CLI Tool

Async Python tool for downloading and comparing performance test videos from Mozilla CI. Supports both **browsertime/raptor** (tp6 pageload) and **mozperftest Android startup** (applink, homeview, shopify, etc.) tests.

## Installation

```bash
uv sync
```

## Usage

### Perfcompare URL (recommended)

```bash
uv run python perf_sxs.py "https://perf.compare/compare-results?baseRev=BASE&newRev=NEW&..."
```

The tool automatically fetches high-confidence tests from Treeherder (frameworks 13 + 15 in parallel) and downloads only those videos. Works for both browsertime and mozperftest comparisons, including mixed perfcompare links.

```bash
# Download all tests, ignore confidence filter
uv run python perf_sxs.py "https://perf.compare/..." --all-tests

# Download all tests but narrow to specific ones
uv run python perf_sxs.py "https://perf.compare/..." --all-tests --tests applink

# Download all runs per test (default: median only)
uv run python perf_sxs.py "https://perf.compare/..." --all-runs
```

### Lando URL

```bash
uv run python perf_sxs.py \
    "https://perf.compare/compare-lando-results?baseLando=181700&newLando=181701&baseRepo=try&newRepo=try&framework=13"
```

Lando IDs are resolved to revision hashes via the Lando API. If the job is still pending, wait ~30s and retry.

### Two revisions

```bash
uv run python perf_sxs.py \
    881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8 \
    56290454af1890c3344757213fc7199839fe3e7f
```

### Single revision (no comparison)

```bash
uv run python perf_sxs.py 881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8 --no-compare
```

Shows a single full-width video panel instead of side-by-side.

### View previously downloaded videos

```bash
uv run python viewer.py ./sxs_videos
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--platforms`, `-p` | Comma-separated platform filters (e.g., `linux,a55`) | All |
| `--tests`, `-t` | Comma-separated test name filters (e.g., `amazon,applink`) | All |
| `--output`, `-o` | Output directory | `./sxs_videos` |
| `--max-downloads`, `-m` | Concurrent downloads | 10 |
| `--no-serve` | Skip auto-launching viewer | false |
| `--all-tests` | Skip High confidence filter | false |
| `--all-runs` | Download all runs (default: median only) | false |
| `--no-compare` | Single revision mode | false |
| `--confidence-json` | Local perfcompare JSON for confidence filtering | None |
| `--port` | Viewer port | 3333 |

## How It Works

1. **Parse input** — perfcompare URL, lando URL (resolved via Lando API), Treeherder URL, or plain revision hash
2. **Find task groups** — queries TC index; mozilla-central can have multiple task groups per revision (CI + perf pushes), all are fetched and merged
3. **Fetch high-confidence tests** — queries Treeherder frameworks 13 (browsertime) and 15 (mozperftest) in parallel; union of results
4. **Filter tasks** — completed browsertime or perftest-startup tasks, deduplicated by test/platform
5. **Download artifacts** — tries `public/test_info/browsertime-videos-annotated.tgz` first; for perftest tasks falls back to TC artifact listing to find the archive (e.g. `public/build/<test>.tgz`)
6. **Median selection** — downloads `perfherder-data.json` per task to pick the run closest to the median; deletes others unless `--all-runs`
7. **Extract & organize** — extracts archives, groups screenshots with their video run, organizes into `base/` and `new/`
8. **Generate metadata** — writes `comparisons.json`
9. **Launch viewer** — auto-opens browser

## Viewer Features

- **Side-by-side synchronized playback** — auto-plays on test selection and run change
- **Screenshots panel** — collapsible panel below videos showing PNG screenshots from the same run (Android startup tests); click any image to fullscreen
- **Run selector** — switches both video and screenshots together; median labeled by default
- **Sidebar search** — Fuse.js fuzzy search with extended syntax (`'exact`, `^prefix`, `!negate`, `a | b`)
- **Speed control** — 0.25x–2x
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
