"""Tests for perf_sxs.cli: argument parsing and the --json output helpers.

Run with: pytest tests/test_cli.py
Or with uv: uv run pytest tests/test_cli.py

These tests avoid exercising `_amain`'s full async flow (which talks to
Taskcluster/Treeherder/Lando over the network) and instead target the
pieces that are cleanly unit-testable in isolation: argparse wiring, the
`log()` stdout/stderr router, and the `build_error_json()` shape used on
every --json error path.
"""

import json

import pytest

from perf_sxs.cli import build_error_json, build_parser, log


class TestJsonFlagParsing:
    """--json should be a plain boolean flag, defaulting to off."""

    def test_json_flag_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["881d2bbfaf53", "56290454af18"])
        assert args.json is False

    def test_json_flag_enabled(self):
        parser = build_parser()
        args = parser.parse_args(["881d2bbfaf53", "56290454af18", "--json"])
        assert args.json is True

    def test_json_flag_combines_with_other_options(self):
        parser = build_parser()
        args = parser.parse_args(
            ["https://perf.compare/compare-results?baseRev=a&newRev=b", "--json", "--no-serve"]
        )
        assert args.json is True
        assert args.no_serve is True


class TestBuildErrorJson:
    """Pure function used on every --json error exit path in _amain."""

    def test_shape_matches_perftest_brain_convention(self):
        result = build_error_json("something went wrong")
        assert result == {"error": "something went wrong", "exit_code": 1}

    def test_is_json_serializable(self):
        result = build_error_json("no matching video tasks found")
        # Round-trips cleanly and produces a single JSON object, matching
        # what every --json error path prints to stdout.
        assert json.loads(json.dumps(result)) == result

    def test_message_is_preserved_verbatim(self):
        message = "Expected 1 perfcompare URL or 2 revisions, got 3 arguments"
        assert build_error_json(message)["error"] == message


class TestLogHelper:
    """log() must route to stderr in --json mode and stdout otherwise."""

    def test_human_mode_writes_stdout(self, capsys):
        log("hello", json_mode=False)
        captured = capsys.readouterr()
        assert captured.out == "hello\n"
        assert captured.err == ""

    def test_json_mode_writes_stderr(self, capsys):
        log("hello", json_mode=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "hello\n"


@pytest.mark.unit
class TestJsonFlagParsingMarked(TestJsonFlagParsing):
    """--json parsing tests (marked as unit tests)."""

    pass


@pytest.mark.unit
class TestBuildErrorJsonMarked(TestBuildErrorJson):
    """build_error_json tests (marked as unit tests)."""

    pass


@pytest.mark.unit
class TestLogHelperMarked(TestLogHelper):
    """log() helper tests (marked as unit tests)."""

    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
