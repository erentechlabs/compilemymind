import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import release_gate  # noqa: E402


class ReleaseGateTests(unittest.TestCase):
    def test_publish_requires_content_for_published_result(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "publish-result.json"
            marker.write_text(json.dumps({"result": "published"}), encoding="utf-8")
            with patch.object(release_gate, "PUBLISH_RESULT", marker), \
                patch.object(release_gate, "status_paths", return_value=[".autopublisher/state.json"]), \
                patch.object(release_gate, "write_result") as write_result, \
                patch.object(release_gate, "run_validation") as run_validation, \
                patch.object(sys, "argv", ["release_gate.py", "--mode", "publish"]):
                self.assertEqual(release_gate.main(), 1)
            run_validation.assert_not_called()
            write_result.assert_called_once()

    def test_publish_without_approved_content_is_a_safe_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "publish-result.json"
            marker.write_text(json.dumps({"result": "rejected"}), encoding="utf-8")
            with patch.object(release_gate, "PUBLISH_RESULT", marker), \
                patch.object(release_gate, "status_paths", return_value=[".autopublisher/state.json"]), \
                patch.object(release_gate, "write_result") as write_result, \
                patch.object(sys, "argv", ["release_gate.py", "--mode", "publish"]):
                self.assertEqual(release_gate.main(), 0)
            write_result.assert_called_once()

    def test_allowed_path_matching_supports_directories(self):
        self.assertTrue(release_gate.path_allowed("content/posts/example/index.md", ("content/posts/",)))
        self.assertTrue(release_gate.path_allowed(".hugo-version", (".hugo-version",)))
        self.assertFalse(release_gate.path_allowed("README.md", ("content/posts/",)))

    def test_rendered_audit_report_is_an_expected_validation_artifact(self):
        for mode in release_gate.ALLOWED_PATHS:
            allowed = release_gate.ALLOWED_PATHS[mode] + (release_gate.RENDERED_AUDIT_REPORT,)
            self.assertTrue(release_gate.path_allowed(release_gate.RENDERED_AUDIT_REPORT, allowed))


if __name__ == "__main__":
    unittest.main()
