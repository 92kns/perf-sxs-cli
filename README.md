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

### Download videos from two Try pushes

```bash
uv run python perf_sxs.py \
    "https://treeherder.mozilla.org/jobs?repo=try&revision=BASE_REV" \
    "https://treeherder.mozilla.org/jobs?repo=try&revision=NEW_REV" \
    --output ./videos
```

### Filter by platform and test

```bash
uv run python perf_sxs.py <base-url> <new-url> \
    --platforms linux,windows,macos \
    --tests amazon,google,facebook \
    --max-downloads 20
```

### Auto-launch viewer

```bash
uv run python perf_sxs.py <base-url> <new-url> --serve
```

### View previously downloaded videos

```bash
uv run python viewer.py ./videos --port 5000
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--platforms`, `-p` | Comma-separated platform filters (e.g., `linux,windows`) | All platforms |
| `--tests`, `-t` | Comma-separated test name filters (e.g., `amazon,cnn`) | All tests |
| `--output`, `-o` | Output directory | `./sxs_videos` |
| `--max-downloads`, `-m` | Concurrent downloads | 10 |
| `--serve` | Auto-start viewer after download | false |
| `--port` | Viewer port | 5000 |

## Examples

### Download all Linux tp6 tests
```bash
uv run python perf_sxs.py <base> <new> --platforms linux --tests tp6
```

### Download specific test with high concurrency
```bash
uv run python perf_sxs.py <base> <new> \
    --tests amazon \
    --max-downloads 30 \
    --serve
```

## How It Works

1. **Parse Try URLs** - Extracts revisions from Treeherder URLs
2. **Find Task Groups** - Queries TaskCluster index API
3. **Filter Tasks** - Finds completed browsertime tests, deduplicates by test/platform
4. **Download Videos** - Async downloads with `aiohttp` (configurable concurrency)
5. **Extract & Organize** - Extracts tar.gz archives, organizes by base/new
6. **Generate Metadata** - Creates `comparisons.json` for viewer

## Viewer Features

- **Side-by-side playback** - Synchronized base vs new videos
- **Playback controls** - Play/pause/restart both videos together
- **Speed adjustment** - 0.25x to 2x playback speed
- **Run selection** - Switch between different test runs
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

## Requirements

- Python 3.8+
- `aiohttp` >= 3.9.0
- `flask` >= 2.0.0
