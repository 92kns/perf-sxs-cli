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

from flask import Flask, jsonify, render_template, send_file


def create_app(video_dir: Path) -> Flask:
    """Create and configure the Flask app."""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    video_dir = Path(video_dir)
    meta_path = video_dir / "comparisons.json"

    if meta_path.exists():
        with open(meta_path) as f:
            metadata = json.load(f)
    else:
        metadata = {"base_revision": None, "new_revision": None, "comparisons": {}}

    def load_analysis() -> dict:
        analysis_path = video_dir / "analysis.json"
        if analysis_path.exists():
            with open(analysis_path) as f:
                return json.load(f)
        return {}

    @app.route("/")
    def index():
        analysis = load_analysis()
        return render_template(
            "viewer.html",
            mode=metadata.get("mode", "compare"),
            base_revision=metadata.get("base_revision"),
            new_revision=metadata.get("new_revision"),
            comparisons=metadata.get("comparisons", {}),
            comparisons_json=json.dumps(metadata.get("comparisons", {})),
            analysis=analysis.get("comparisons", {}),
            analysis_json=json.dumps(analysis.get("comparisons", {})),
        )

    @app.route("/api/analysis")
    def api_analysis():
        return jsonify(load_analysis())

    @app.route("/video/<path:video_path>")
    def serve_video(video_path):
        try:
            # Resolve path relative to video_dir
            full_path = (video_dir / video_path).resolve()

            # Security: ensure resolved path is within video_dir
            if not full_path.is_relative_to(video_dir.resolve()):
                return "Access denied", 403

            if full_path.exists() and full_path.is_file():
                return send_file(full_path)
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
