#!/usr/bin/env python3
"""
Side-by-side video comparison viewer for browsertime tests.

Usage:
    perf-sxs-viewer [video_dir] [--port PORT]
"""

import argparse
import json
import os
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, send_file

PACKAGE_DIR = Path(__file__).parent


def create_app(video_dir: Path) -> Flask:
    """Create and configure the Flask app."""
    app = Flask(
        __name__,
        template_folder=str(PACKAGE_DIR / "templates"),
        static_folder=str(PACKAGE_DIR / "static"),
    )

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Side-by-side video comparison viewer")
    parser.add_argument(
        "video_dir",
        nargs="?",
        default="./sxs_videos",
        help="Directory containing downloaded videos",
    )
    parser.add_argument("--port", "-p", type=int, default=3333, help="Port to serve on")
    parser.add_argument(
        "--host",
        "-H",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1; use 0.0.0.0 to expose on your LAN)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON summary ({'serving': true, 'url': ...}) before serving, "
        "and skip auto-launching a browser tab",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    video_dir = Path(args.video_dir)
    if not video_dir.exists():
        message = f"Directory {video_dir} does not exist"
        if args.json:
            print(json.dumps({"error": message, "exit_code": 1}))
        else:
            print(f"Error: {message}")
            print("Run perf-sxs first to download videos")
        return 1

    app = create_app(video_dir)
    url = f"http://{args.host}:{args.port}"

    if args.json:
        # Machine-readable summary first, printed before app.run() blocks.
        print(json.dumps({"serving": True, "url": url}))
    else:
        print(f"Starting viewer at {url}")
        print(f"Video directory: {video_dir.absolute()}")

    if not args.json and os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
