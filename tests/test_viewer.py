"""
Tests for viewer.py Flask application.

Run with: pytest tests/test_viewer.py
Or with uv: uv run pytest tests/test_viewer.py
"""

import json

# Import viewer module
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from viewer import create_app


@pytest.fixture
def test_video_dir():
    """Create a temporary directory with test video structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create directory structure
        base_dir = (
            tmpdir
            / "base"
            / "test-platform_opt"
            / "browsertime-test"
            / "task123"
            / "browsertime-videos-original"
        )
        base_dir.mkdir(parents=True)

        new_dir = (
            tmpdir
            / "new"
            / "test-platform_opt"
            / "browsertime-test"
            / "task456"
            / "browsertime-videos-original"
        )
        new_dir.mkdir(parents=True)

        # Create dummy video files
        (base_dir / "video1.mp4").write_text("fake base video")
        (base_dir / "video2.mp4").write_text("fake base video 2")
        (new_dir / "video1.mp4").write_text("fake new video")
        (new_dir / "video2.mp4").write_text("fake new video 2")

        # Create comparisons.json
        comparisons = {
            "base_revision": "abc123",
            "new_revision": "def456",
            "comparisons": {
                "test-platform_opt/browsertime-test": {
                    "platform": "test-platform_opt",
                    "test_name": "browsertime-test",
                    "base_videos": [
                        "base/test-platform_opt/browsertime-test/task123/browsertime-videos-original/video1.mp4",
                        "base/test-platform_opt/browsertime-test/task123/browsertime-videos-original/video2.mp4",
                    ],
                    "new_videos": [
                        "new/test-platform_opt/browsertime-test/task456/browsertime-videos-original/video1.mp4",
                        "new/test-platform_opt/browsertime-test/task456/browsertime-videos-original/video2.mp4",
                    ],
                }
            },
        }

        with open(tmpdir / "comparisons.json", "w") as f:
            json.dump(comparisons, f)

        yield tmpdir


@pytest.fixture
def client(test_video_dir):
    """Create Flask test client."""
    app = create_app(test_video_dir)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestViewerApp:
    """Test Flask viewer application."""

    def test_index_route(self, client):
        """Test that index route returns HTML."""
        response = client.get("/")
        assert response.status_code == 200
        assert b"<!DOCTYPE html>" in response.data
        assert b"Perf Side-by-Side Viewer" in response.data

    def test_index_contains_revision_info(self, client):
        """Test that index displays revision information."""
        response = client.get("/")
        assert response.status_code == 200
        # Check for revision hashes in HTML
        assert b"abc123" in response.data  # base revision
        assert b"def456" in response.data  # new revision

    def test_metadata_route(self, client):
        """Test metadata API endpoint."""
        response = client.get("/api/comparisons")
        assert response.status_code == 200

        data = json.loads(response.data)
        assert "base_revision" in data
        assert "new_revision" in data
        assert "comparisons" in data
        assert data["base_revision"] == "abc123"
        assert data["new_revision"] == "def456"

    def test_metadata_comparisons_structure(self, client):
        """Test metadata comparisons have correct structure."""
        response = client.get("/api/comparisons")
        data = json.loads(response.data)

        comparisons = data["comparisons"]
        assert len(comparisons) == 1

        key = "test-platform_opt/browsertime-test"
        assert key in comparisons

        comparison = comparisons[key]
        assert "platform" in comparison
        assert "test_name" in comparison
        assert "base_videos" in comparison
        assert "new_videos" in comparison
        assert len(comparison["base_videos"]) == 2
        assert len(comparison["new_videos"]) == 2

    def test_video_route(self, client, test_video_dir):
        """Test video serving endpoint."""
        video_path = (
            "base/test-platform_opt/browsertime-test/task123/browsertime-videos-original/video1.mp4"
        )
        response = client.get(f"/video/{video_path}")

        assert response.status_code == 200
        assert response.data == b"fake base video"

    def test_video_route_not_found(self, client):
        """Test video route returns 404 for non-existent videos."""
        response = client.get("/video/nonexistent/path/video.mp4")
        assert response.status_code == 404


class TestViewerWithEmptyDirectory:
    """Test viewer behavior with missing or empty directories."""

    def test_missing_comparisons_file(self):
        """Test viewer handles missing comparisons.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # No comparisons.json file
            app = create_app(tmpdir)
            app.config["TESTING"] = True

            with app.test_client() as client:
                response = client.get("/api/comparisons")
                assert response.status_code == 200

                data = json.loads(response.data)
                # Should have default empty structure
                assert "comparisons" in data
                assert isinstance(data["comparisons"], dict)

    def test_empty_comparisons_file(self):
        """Test viewer handles empty comparisons."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create empty comparisons.json
            comparisons = {"base_revision": "", "new_revision": "", "comparisons": {}}

            with open(tmpdir / "comparisons.json", "w") as f:
                json.dump(comparisons, f)

            app = create_app(tmpdir)
            app.config["TESTING"] = True

            with app.test_client() as client:
                response = client.get("/api/comparisons")
                assert response.status_code == 200

                data = json.loads(response.data)
                assert len(data["comparisons"]) == 0


class TestVideoPathSecurity:
    """Test that video paths are properly validated."""

    def test_video_path_traversal_blocked(self, client):
        """Test that path traversal attempts are blocked."""
        malicious_paths = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32\\config\\sam",
            "base/../../sensitive_file.txt",
        ]

        for path in malicious_paths:
            response = client.get(f"/video/{path}")
            # Should either return 404 or 400, not serve the file
            assert response.status_code in [404, 400]


# Pytest markers
@pytest.mark.unit
class TestViewerAppMarked(TestViewerApp):
    """Viewer app tests (marked as unit tests)."""

    pass


@pytest.mark.integration
class TestViewerWithEmptyDirectoryMarked(TestViewerWithEmptyDirectory):
    """Empty directory tests (marked as integration tests)."""

    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
