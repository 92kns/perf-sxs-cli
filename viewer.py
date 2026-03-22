#!/usr/bin/env python3
"""
Side-by-side video comparison viewer for browsertime tests.

Usage:
    python viewer.py [video_dir] [--port PORT]
"""

import argparse
import json
import os
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template_string, send_file

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Perf Side-by-Side Viewer</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        header {
            background: #16213e;
            padding: 1rem 2rem;
            border-bottom: 1px solid #0f3460;
        }
        header h1 { font-size: 1.5rem; font-weight: 500; }
        .revision-info {
            display: flex;
            gap: 2rem;
            margin-top: 0.5rem;
            font-size: 0.9rem;
            color: #888;
        }
        .revision-info span { font-family: monospace; color: #e94560; }
        .container {
            display: flex;
            min-height: calc(100vh - 80px);
        }
        .sidebar {
            width: 300px;
            background: #16213e;
            border-right: 1px solid #0f3460;
            overflow-y: auto;
            padding: 1rem 0;
            max-height: calc(100vh - 80px);
        }
        .sidebar h2 {
            padding: 0.5rem 1rem;
            font-size: 0.8rem;
            text-transform: uppercase;
            color: #666;
            letter-spacing: 0.05em;
        }
        .sidebar-search {
            padding: 0.5rem 1rem;
            border-bottom: 1px solid #0f3460;
        }
        .sidebar-search input {
            width: 100%;
            background: #0f3460;
            border: none;
            color: #eee;
            padding: 0.4rem 0.6rem;
            border-radius: 4px;
            font-size: 0.85rem;
        }
        .sidebar-search input::placeholder { color: #555; }
        .sidebar-search input:focus { outline: 1px solid #4ecca3; }
        .platform-section {
            margin-bottom: 1.5rem;
        }
        .platform-section h3 {
            padding: 0.75rem 1rem;
            font-size: 0.85rem;
            font-weight: 600;
            color: #4ecca3;
            background: #0f3460;
            border-left: 3px solid #4ecca3;
            margin-bottom: 0.5rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: space-between;
            user-select: none;
        }
        .platform-section h3:hover {
            background: #1a1a2e;
        }
        .platform-section h3 .toggle-icon {
            font-size: 0.7rem;
            transition: transform 0.2s;
        }
        .platform-section.collapsed h3 .toggle-icon {
            transform: rotate(-90deg);
        }
        .platform-section .test-list {
            max-height: 1000px;
            overflow: hidden;
            transition: max-height 0.3s ease-out, opacity 0.2s;
            opacity: 1;
        }
        .platform-section.collapsed .test-list {
            max-height: 0;
            opacity: 0;
        }
        .test-item {
            padding: 0.75rem 1rem;
            cursor: pointer;
            border-left: 3px solid transparent;
            transition: all 0.2s;
        }
        .test-item:hover { background: #1a1a2e; }
        .test-item.active {
            background: #1a1a2e;
            border-left-color: #e94560;
        }
        .test-item .platform {
            font-size: 0.75rem;
            color: #666;
            margin-bottom: 0.25rem;
        }
        .test-item .name {
            font-size: 0.9rem;
            word-break: break-word;
        }
        .main {
            flex: 1;
            padding: 2rem;
            overflow-y: auto;
        }
        .comparison-view {
            display: none;
        }
        .comparison-view.active {
            display: block;
        }
        .video-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
            margin-bottom: 1rem;
        }
        .video-panel {
            background: #16213e;
            border-radius: 8px;
            overflow: hidden;
        }
        .video-panel h3 {
            padding: 0.75rem 1rem;
            font-size: 0.9rem;
            font-weight: 500;
            border-bottom: 1px solid #0f3460;
        }
        .video-panel.base h3 { color: #4ecca3; }
        .video-panel.new h3 { color: #e94560; }
        .video-panel video {
            width: 100%;
            max-height: calc(100vh - 300px);
            display: block;
            object-fit: contain;
            background: #000;
        }
        .controls {
            background: #16213e;
            border-radius: 8px;
            padding: 1rem;
            display: flex;
            gap: 1rem;
            align-items: center;
            flex-wrap: wrap;
        }
        .controls button {
            background: #0f3460;
            border: none;
            color: #eee;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: background 0.2s;
        }
        .controls button:hover { background: #e94560; }
        .controls button.active { background: #e94560; }
        .controls label {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.9rem;
        }
        .controls input[type="range"] {
            width: 150px;
        }
        .video-select {
            display: flex;
            gap: 0.5rem;
            align-items: center;
        }
        .video-select select {
            background: #0f3460;
            border: none;
            color: #eee;
            padding: 0.5rem;
            border-radius: 4px;
        }
        .empty-state {
            text-align: center;
            padding: 4rem 2rem;
            color: #666;
        }
        .empty-state h2 { margin-bottom: 1rem; }
    </style>
</head>
<body>
    <header>
        <h1>Perf Side-by-Side Viewer</h1>
        <div class="revision-info">
            <div>Base: <span id="base-rev">{{ base_revision[:12] if base_revision else 'N/A' }}</span></div>
            {% if mode != 'single' %}
            <div>New: <span id="new-rev">{{ new_revision[:12] if new_revision else 'N/A' }}</span></div>
            {% endif %}
        </div>
    </header>

    <div class="container">
        <div class="sidebar">
            <h2>Tests (<span id="test-count">{{ comparisons|length }}</span>)</h2>
            <div class="sidebar-search">
                <input type="text" id="test-search" placeholder="Filter tests..." oninput="filterTests(this.value)">
            </div>
            <div style="padding: 0.5rem 1rem; border-bottom: 1px solid #0f3460;">
                <label style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; cursor: pointer;">
                    <input type="checkbox" id="auto-play-toggle" onchange="toggleAutoPlay(this.checked)" checked>
                    Auto-play on select
                </label>
            </div>
            {% set platforms = {} %}
            {% for key, comp in comparisons.items() %}
                {% if comp.platform not in platforms %}
                    {% set _ = platforms.update({comp.platform: []}) %}
                {% endif %}
                {% set _ = platforms[comp.platform].append((key, comp)) %}
            {% endfor %}
            {% for platform in platforms|sort %}
            <div class="platform-section">
                <h3 onclick="togglePlatform('{{ platform }}')">
                    <span>{{ platform }} ({{ platforms[platform]|length }})</span>
                    <span class="toggle-icon">▼</span>
                </h3>
                <div class="test-list">
                    {% for key, comp in platforms[platform] %}
                    <div class="test-item" data-key="{{ key }}" onclick="selectTest('{{ key }}')">
                        <div class="name">{{ comp.test_name }}</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="main">
            {% if not comparisons %}
            <div class="empty-state">
                <h2>No comparisons available</h2>
                <p>Download videos first using perf_sxs.py</p>
            </div>
            {% else %}
            <div class="empty-state" id="placeholder">
                <h2>Select a test from the sidebar</h2>
            </div>

            {% for key, comp in comparisons.items() %}
            <div class="comparison-view" id="view-{{ key|replace('/', '-') }}">
                <div class="controls">
                    <button onclick="playBoth('{{ key }}')" id="play-btn-{{ key|replace('/', '-') }}">Play Both</button>
                    <button onclick="pauseBoth('{{ key }}')">Pause</button>
                    <button onclick="restartBoth('{{ key }}')">Restart</button>
                    <label>
                        Speed:
                        <input type="range" min="0.25" max="2" step="0.25" value="1"
                               onchange="setSpeed('{{ key }}', this.value)">
                        <span id="speed-{{ key|replace('/', '-') }}">1x</span>
                    </label>
                    <label>
                        <input type="checkbox" onchange="toggleSync('{{ key }}', this.checked)" checked>
                        Sync playback
                    </label>
                    {% if comp.base_videos|length > 1 %}
                    <div class="video-select">
                        <label>Run:</label>
                        <select onchange="selectRun('{{ key }}', this.value)" id="run-select-{{ key|replace('/', '-') }}">
                            {% for i in range(comp.base_videos|length) %}
                            <option value="{{ i }}"
                                {% if i == comp.base_median_idx %}selected{% endif %}>
                                Run {{ i + 1 }}{% if i == comp.base_median_idx %} (median){% endif %}
                            </option>
                            {% endfor %}
                        </select>
                    </div>
                    {% endif %}
                </div>

                <div class="video-container" style="{{ 'grid-template-columns: 1fr;' if mode == 'single' else '' }}">
                    <div class="video-panel base">
                        <h3>{{ base_revision[:12] if base_revision else 'N/A' }}</h3>
                        <video id="base-{{ key|replace('/', '-') }}"
                               src="/video/{{ comp.base_videos[comp.base_median_idx or 0]|urlencode }}"
                               muted></video>
                    </div>
                    {% if mode != 'single' %}
                    <div class="video-panel new">
                        <h3>New ({{ new_revision[:12] if new_revision else 'N/A' }})</h3>
                        <video id="new-{{ key|replace('/', '-') }}"
                               src="/video/{{ comp.new_videos[comp.new_median_idx or 0]|urlencode }}"
                               muted></video>
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
            {% endif %}
        </div>
    </div>

    <script>
        const comparisons = {{ comparisons_json|safe }};
        const mode = {{ mode|tojson }};
        let syncEnabled = {};
        let currentTest = null;
        let autoPlayEnabled = true;

        function selectTest(key) {
            const safeKey = key.replace('/', '-');

            // Update sidebar
            document.querySelectorAll('.test-item').forEach(el => el.classList.remove('active'));
            document.querySelector(`.test-item[data-key="${key}"]`).classList.add('active');

            // Update main view
            document.getElementById('placeholder')?.classList.add('comparison-view');
            document.querySelectorAll('.comparison-view').forEach(el => el.classList.remove('active'));
            document.getElementById(`view-${safeKey}`).classList.add('active');

            currentTest = key;

            // Auto-play if enabled
            if (autoPlayEnabled) {
                setTimeout(() => playBoth(key), 100);
            }
        }

        function playBoth(key) {
            const safeKey = key.replace('/', '-');
            const baseVideo = document.getElementById(`base-${safeKey}`);
            const newVideo = document.getElementById(`new-${safeKey}`);

            if (newVideo && syncEnabled[key] !== false) {
                const startTime = Math.max(baseVideo.currentTime, newVideo.currentTime);
                baseVideo.currentTime = startTime;
                newVideo.currentTime = startTime;
            }

            baseVideo.play();
            if (newVideo) newVideo.play();
        }

        function pauseBoth(key) {
            const safeKey = key.replace('/', '-');
            document.getElementById(`base-${safeKey}`).pause();
            document.getElementById(`new-${safeKey}`)?.pause();
        }

        function restartBoth(key) {
            const safeKey = key.replace('/', '-');
            const baseVideo = document.getElementById(`base-${safeKey}`);
            const newVideo = document.getElementById(`new-${safeKey}`);

            baseVideo.currentTime = 0;
            baseVideo.play();
            if (newVideo) { newVideo.currentTime = 0; newVideo.play(); }
        }

        function setSpeed(key, speed) {
            const safeKey = key.replace('/', '-');
            document.getElementById(`base-${safeKey}`).playbackRate = speed;
            const newVideo = document.getElementById(`new-${safeKey}`);
            if (newVideo) newVideo.playbackRate = speed;
            document.getElementById(`speed-${safeKey}`).textContent = speed + 'x';
        }

        function toggleSync(key, enabled) {
            syncEnabled[key] = enabled;
        }

        function selectRun(key, index) {
            const safeKey = key.replace('/', '-');
            const comp = comparisons[key];

            if (comp.base_videos[index]) {
                document.getElementById(`base-${safeKey}`).src = '/video/' + encodeURIComponent(comp.base_videos[index]);
            }
            const newVideo = document.getElementById(`new-${safeKey}`);
            if (newVideo && comp.new_videos && comp.new_videos[index]) {
                newVideo.src = '/video/' + encodeURIComponent(comp.new_videos[index]);
            }
        }

        function togglePlatform(platform) {
            const section = event.target.closest('.platform-section');
            section.classList.toggle('collapsed');
        }

        function toggleAutoPlay(enabled) {
            autoPlayEnabled = enabled;
        }

        function filterTests(query) {
            const q = query.toLowerCase();
            let visible = 0;
            document.querySelectorAll('.test-item').forEach(el => {
                const match = !q || el.dataset.key.toLowerCase().includes(q);
                el.style.display = match ? '' : 'none';
                if (match) visible++;
            });
            // Show/hide platform sections based on whether any children are visible
            document.querySelectorAll('.platform-section').forEach(section => {
                const anyVisible = [...section.querySelectorAll('.test-item')]
                    .some(el => el.style.display !== 'none');
                section.style.display = anyVisible ? '' : 'none';
                if (anyVisible && q) section.classList.remove('collapsed');
            });
            document.getElementById('test-count').textContent = q
                ? `${visible}/{{ comparisons|length }}`
                : '{{ comparisons|length }}';
        }

        // Select first test by default
        const firstKey = Object.keys(comparisons)[0];
        if (firstKey) {
            selectTest(firstKey);
        }
    </script>
</body>
</html>
"""


def create_app(video_dir: Path) -> Flask:
    """Create and configure the Flask app."""
    app = Flask(__name__)

    video_dir = Path(video_dir)
    meta_path = video_dir / "comparisons.json"

    if meta_path.exists():
        with open(meta_path) as f:
            metadata = json.load(f)
    else:
        metadata = {"base_revision": None, "new_revision": None, "comparisons": {}}

    @app.route("/")
    def index():
        return render_template_string(
            HTML_TEMPLATE,
            mode=metadata.get("mode", "compare"),
            base_revision=metadata.get("base_revision"),
            new_revision=metadata.get("new_revision"),
            comparisons=metadata.get("comparisons", {}),
            comparisons_json=json.dumps(metadata.get("comparisons", {})),
        )

    @app.route("/video/<path:video_path>")
    def serve_video(video_path):
        try:
            # Resolve path relative to video_dir
            full_path = (video_dir / video_path).resolve()

            # Security: ensure resolved path is within video_dir
            if not full_path.is_relative_to(video_dir.resolve()):
                return "Access denied", 403

            if full_path.exists() and full_path.is_file():
                return send_file(full_path, mimetype="video/mp4")
            return "Video not found", 404
        except (ValueError, OSError):
            return "Invalid path", 400

    @app.route("/api/comparisons")
    def api_comparisons():
        return jsonify(metadata)

    return app


def main():
    parser = argparse.ArgumentParser(description="Side-by-side video comparison viewer")
    parser.add_argument(
        "video_dir",
        nargs="?",
        default="./sxs_videos",
        help="Directory containing downloaded videos",
    )
    parser.add_argument("--port", "-p", type=int, default=3333, help="Port to serve on")
    parser.add_argument("--host", "-H", default="0.0.0.0", help="Host to bind to")

    args = parser.parse_args()

    video_dir = Path(args.video_dir)
    if not video_dir.exists():
        print(f"Error: Directory {video_dir} does not exist")
        print("Run perf_sxs.py first to download videos")
        return 1

    app = create_app(video_dir)
    url = f"http://localhost:{args.port}"
    print(f"Starting viewer at {url}")
    print(f"Video directory: {video_dir.absolute()}")

    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
