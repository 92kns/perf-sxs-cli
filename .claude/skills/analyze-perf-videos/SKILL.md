---
name: analyze-perf-videos
description: Analyze perf-sxs base vs new video comparisons and explain visible regressions. Use when asked to analyze, review, or explain perf-sxs / browsertime videos.
allowed-tools: Bash, Read, Write
context: fork
argument-hint: "[video_dir] [test_filters]"
---

You are the judge in a three-step pipeline. `perf-sxs-analyze` does the measuring and the reporting; you look at contact sheets and say what is visibly different. You need no API key.

`$ARGUMENTS`: an optional video directory (default `./sxs_videos`) and optional comma-separated filters (substrings of comparison keys, e.g. `amazon,cnn`). A token that is an existing directory is the directory; the rest are filters.

## 1. Prepare

```bash
uv run perf-sxs-analyze prepare <video_dir> [-t <filters>]
```

(`perf-sxs-analyze` works directly too if it was installed with `uv tool install`.) It needs ffmpeg; if it reports ffmpeg missing, tell the user and stop. Done when `<video_dir>/analysis/manifest.json` exists. Note any comparisons it lists as failed.

## 2. Judge

Read `<video_dir>/analysis/instructions.md`. That file holds the judging rules: how a contact sheet is laid out, what the numbers mean, and the verdict and severity scale. Apply them exactly as written.

Then open `manifest.json`. Comparisons are ordered most-changed first. For each one that has a `sheet`:

1. Read its `context` text (the measurements and perfcompare rows).
2. Read the image at `<video_dir>/<sheet>`.
3. Record a judgment: `summary`, `verdict`, `severity`, `observations`.

If there are more than 30, judge the first 30 and tell the user how many you skipped and that filters narrow the set. Done when every comparison you took on has a judgment grounded in what its sheet shows.

Write them all to `<video_dir>/analysis/judgments.json`, keyed by comparison key:

```json
{"<comparison key>": {"summary": "...", "verdict": "regression", "severity": "medium", "observations": ["..."]}}
```

## 3. Report

```bash
uv run perf-sxs-analyze report <video_dir> --judgments <video_dir>/analysis/judgments.json --model "<your model name>"
```

This writes `analysis.json` (the viewer's analysis panel) and a self-contained `analysis_report.html` that can be attached to a bug.

Reply to the user with:
- the verdict counts;
- the top findings, one line each;
- any comparison where base and new showed different page content;
- the two output paths, and a reminder to refresh the viewer.
