import unittest

from pathlib import Path
import tempfile

from infra_maintenance import infrastructure_inventory, release_note_risk_terms, version_risk


class InfrastructureMaintenanceTests(unittest.TestCase):
    def test_equal_versions_are_unchanged(self):
        self.assertEqual(version_risk("0.163.1", "0.163.1"), "none")

    def test_only_patch_updates_are_automatic_candidates(self):
        self.assertEqual(version_risk("0.163.1", "0.163.2"), "low")
        self.assertEqual(version_risk("0.163.1", "0.164.0"), "medium")

    def test_major_updates_remain_manual_candidates(self):
        self.assertEqual(version_risk("0.164.0", "1.0.0"), "high")

    def test_release_note_breaking_signals_require_review(self):
        self.assertEqual(
            release_note_risk_terms("This release removes a deprecated API and includes migration notes."),
            ["deprecated", "migration"],
        )
        self.assertEqual(release_note_risk_terms("Bug fixes and documentation corrections."), [])

    def test_dependency_inventory_records_runtime_actions_and_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".autopublisher").mkdir()
            (root / ".hugo-version").write_text("0.164.0\n", encoding="utf-8")
            (root / ".github" / "workflows" / "test.yml").write_text(
                "steps:\n  - uses: actions/checkout@v6\n", encoding="utf-8"
            )
            (root / ".autopublisher" / "config.json").write_text(
                '{"gemini":{"text_model":"gemini-test"}}', encoding="utf-8"
            )
            inventory = infrastructure_inventory(root)
        self.assertEqual(inventory["hugo_pin"], "0.164.0")
        self.assertEqual(inventory["workflow_actions"]["test.yml"], ["actions/checkout@v6"])
        self.assertEqual(inventory["gemini_models"]["text_model"], "gemini-test")


if __name__ == "__main__":
    unittest.main()
