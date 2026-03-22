# Perf Side-by-Side CLI Tool

Async Python tool for downloading and comparing browsertime videos from Mozilla Try pushes.

## Installation

### Using uv (recommended)

```bash
uv sync
```

### Using pip

```bash
pip install -r requirements.txt
```

## Usage

### Using perfcompare URL (recommended)

```bash
uv run python perf_sxs.py \
    "https://perf.compare/compare-results?baseRev=BASE&newRev=NEW&..."
```

This is the most convenient method since perfcompare URLs contain both revisions in a single link. The viewer will automatically launch when downloads complete.

**Smart Filtering (Automatic):** When you provide a perfcompare URL, the tool automatically:
1. Calls the Treeherder API to fetch performance comparison data
2. Filters to only tests with `confidence_text: "High"` (statistically significant changes)
3. Downloads only videos for those high-confidence tests

This saves time and disk space by focusing on important regressions/improvements.

```bash
# Automatic high confidence filtering
uv run python perf_sxs.py \
    "https://perf.compare/compare-results?baseRev=BASE&newRev=NEW&..."
```

**Manual JSON file (alternative):** If you prefer to use a local JSON file:
```bash
# 1. Download JSON from perfcompare (click "Download JSON" button)
# 2. Run with --confidence-json flag:
uv run python perf_sxs.py \
    "https://perf.compare/compare-results?..." \
    --confidence-json ./perfcompare.json
```

**Download all tests (no filtering):**
```bash
uv run python perf_sxs.py "https://perf.compare/compare-results?..." --all-tests
```

**Download all runs per test (default is median only):**
```bash
uv run python perf_sxs.py "https://perf.compare/compare-results?..." --all-runs
```

### Single revision (no comparison)

```bash
uv run python perf_sxs.py 881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8 --no-compare
```

Useful when you want to inspect videos from a single push without a baseline. The viewer shows a single full-width panel instead of side-by-side.

### Using a lando perfcompare URL

```bash
uv run python perf_sxs.py \
    "https://perf.compare/compare-lando-results?baseLando=181700&newLando=181701&baseRepo=try&newRepo=try&framework=13"
```

Lando IDs are resolved to revision hashes via the Lando API before proceeding. Note: if the Lando job is still pending (< ~30s after push), the tool will error — wait and retry.

### Using two separate revisions

```bash
uv run python perf_sxs.py \
    881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8 \
    56290454af1890c3344757213fc7199839fe3e7f \
    --output ./videos
```

### Using Treeherder URLs

```bash
uv run python perf_sxs.py \
    "https://treeherder.mozilla.org/jobs?repo=try&revision=BASE_REV" \
    "https://treeherder.mozilla.org/jobs?repo=try&revision=NEW_REV" \
    --output ./videos
```

### Download without launching viewer

```bash
uv run python perf_sxs.py <perfcompare-url> --no-serve

# View later
uv run python viewer.py ./sxs_videos
```

### Filter by platform and test

```bash
uv run python perf_sxs.py <base-url> <new-url> \
    --platforms linux,windows,macos \
    --tests amazon,google,facebook \
    --max-downloads 20
```

### View previously downloaded videos

```bash
uv run python viewer.py ./videos
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--platforms`, `-p` | Comma-separated platform filters (e.g., `linux,windows`) | All platforms |
| `--tests`, `-t` | Comma-separated test name filters (e.g., `amazon,cnn`) | All tests |
| `--output`, `-o` | Output directory | `./sxs_videos` |
| `--max-downloads`, `-m` | Concurrent downloads | 10 |
| `--no-serve` | Skip auto-launching viewer after download | false |
| `--confidence-json` | Path to perfcompare JSON for High confidence filtering | None |
| `--all-tests` | Download all tests (ignore High confidence filter) | false |
| `--all-runs` | Download all runs per test (default: median run only) | false |
| `--no-compare` | Single revision mode — download one revision without a comparison target | false |
| `--port` | Viewer port | 3333 |

## Examples

### Quick start with perfcompare URL
```bash
uv run python perf_sxs.py \
    "https://perf.compare/compare-results?baseRev=881d2bbf...&newRev=56290454..."
```

### Download all Linux tp6 tests
```bash
uv run python perf_sxs.py <perfcompare-url> --platforms linux --tests tp6
```

### Download specific test with high concurrency
```bash
uv run python perf_sxs.py <base-rev> <new-rev> \
    --tests amazon \
    --max-downloads 30
```

## How It Works

1. **Parse Input** - Extracts revisions from perfcompare URLs (including lando), Treeherder URLs, or plain revision strings. Lando IDs are resolved to revision hashes via `api.lando.services.mozilla.com`
2. **Find Task Groups** - Queries TaskCluster index API
3. **Load Confidence Data** - If `--confidence-json` provided, loads high confidence test/platform pairs from local JSON
4. **Filter Tasks** - Finds completed browsertime tests, filters by confidence (if applicable), deduplicates by test/platform
5. **Download Videos** - Async downloads of annotated videos with `aiohttp` (configurable concurrency). Downloads `perfherder-data.json` per task to identify the median run; keeps only that video by default (`--all-runs` to keep all)
6. **Extract & Organize** - Extracts tar.gz archives, organizes by base/new
7. **Generate Metadata** - Creates `comparisons.json` for viewer
8. **Launch Viewer** - Auto-opens browser to side-by-side comparison view

## Viewer Features

- **Side-by-side playback** - Synchronized base vs new videos
- **Single revision mode** - Full-width single panel when using `--no-compare`
- **Playback controls** - Play/pause/restart both videos together
- **Speed adjustment** - 0.25x to 2x playback speed
- **Run selection** - Switch between runs when using `--all-runs`; median run labeled and selected by default
- **Sync toggle** - Option to disable synchronized playback

## Output Structure

```
output_dir/
├── comparisons.json           # Metadata for viewer
├── base/                      # Base revision videos
│   └── <platform>/
│       └── <test-name>/
│           └── <task-id>/
│               └── *.mp4
└── new/                       # New revision videos
    └── <platform>/
        └── <test-name>/
            └── <task-id>/
                └── *.mp4
```

## Performance

- **Async downloads** - Uses `aiohttp` for true async I/O
- **Parallel extraction** - Extracts tar.gz files as they download
- **Deduplication** - Only downloads one task per test/platform combo
- **Typical speeds** - 368 artifacts (~8,000 videos, 3.2 GB) in ~5-10 minutes with 15 concurrent downloads

## Troubleshooting

**No videos downloaded**
- Check that tasks are completed (not pending/running)
- Verify test names contain "browsertime"
- Check platform filters match exactly

**Viewer shows no comparisons**
- Ensure both base and new have the same test/platform combinations
- Check `comparisons.json` for matched pairs

**Download errors**
- Increase timeout or reduce `--max-downloads`
- Check network connection
- Verify Try URLs and revisions exist

## Testing

```bash
# Install dev dependencies
uv sync --extra dev

# Run all tests
uv run pytest test_perf_sxs.py -v

# Run only unit tests
uv run pytest -v -m unit

# Run with coverage
uv run pytest --cov=perf_sxs --cov-report=html
```

## Requirements

- Python 3.8+
- `aiohttp` >= 3.9.0
- `flask` >= 2.0.0
- `pytest` >= 7.0.0 (dev only)
