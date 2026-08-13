import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import infrastructure_maintenance as maintenance  # noqa: E402
import validate_infrastructure_changes as validation  # noqa: E402


class InfrastructureMaintenanceTests(unittest.TestCase):
    def test_repository_has_only_the_infrastructure_workflow(self):
        workflow_dir = maintenance.ROOT / ".github" / "workflows"
        workflows = sorted(path.name for path in workflow_dir.glob("*.yml"))
        self.assertEqual(workflows, ["infrastructure-maintenance.yml"])
        workflow = (workflow_dir / workflows[0]).read_text(encoding="utf-8")
        for forbidden in (
            "tools/autopublisher",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "content/posts",
            "--mode publish",
        ):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("infrastructure_maintenance.py --apply-updates", workflow)
        self.assertIn("sync_cloudflare_hugo.py", workflow)
        self.assertIn(".hugo-version README.md themes/mana/package.json", workflow)

    def test_only_patch_updates_are_safe(self):
        self.assertEqual(maintenance.version_risk("0.164.0", "0.164.1"), "low")
        self.assertEqual(maintenance.version_risk("0.164.0", "0.165.0"), "medium")
        self.assertEqual(maintenance.version_risk("0.164.0", "1.0.0"), "high")

    def test_release_note_risk_terms_require_review(self):
        self.assertEqual(
            maintenance.release_note_risk_terms("Removed a deprecated API; migration required."),
            ["removed", "deprecated", "migration"],
        )

    def test_inventory_contains_only_infrastructure_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "themes" / "mana").mkdir(parents=True)
            (root / ".hugo-version").write_text("0.164.0\n", encoding="utf-8")
            (root / ".github" / "workflows" / "infrastructure-maintenance.yml").write_text(
                "steps:\n  - uses: actions/checkout@v6\n", encoding="utf-8"
            )
            (root / "themes" / "mana" / "package.json").write_text(
                '{"name":"mana","version":"1.0.0","devDependencies":{"prettier":"^3.0.0"}}',
                encoding="utf-8",
            )
            inventory = maintenance.infrastructure_inventory(root)
        self.assertEqual(inventory["hugo_pin"], "0.164.0")
        self.assertEqual(list(inventory["workflow_actions"]), ["infrastructure-maintenance.yml"])
        self.assertEqual(inventory["theme"]["dependencies"], {"prettier": "^3.0.0"})
        self.assertNotIn("gemini_models", inventory)

    def test_validator_allows_only_infrastructure_paths(self):
        self.assertTrue(validation.allowed(".hugo-version"))
        self.assertTrue(validation.allowed("README.md"))
        self.assertTrue(validation.allowed("themes/mana/package-lock.json"))
        self.assertTrue(validation.allowed(".maintenance/reports/report.md"))
        self.assertFalse(validation.allowed("content/posts/new/index.md"))
        self.assertFalse(validation.allowed("hugo.toml"))

    def test_hugo_update_changes_both_readme_version_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(
                "[![Hugo Version](https://img.shields.io/badge/Hugo-v0.164.0-purple)](https://gohugo.io/)\n"
                "* **Framework:** [Hugo](https://gohugo.io/) v0.164.0 (Extended Edition)\n",
                encoding="utf-8",
            )
            maintenance.update_readme_hugo_version("0.165.0", readme)
            updated = readme.read_text(encoding="utf-8")

        self.assertIn("Hugo-v0.165.0-", updated)
        self.assertIn("[Hugo](https://gohugo.io/) v0.165.0", updated)

    def test_validator_rejects_readme_version_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".hugo-version").write_text("0.165.0\n", encoding="utf-8")
            (root / "README.md").write_text(
                "Hugo-v0.164.0-purple\n"
                "* **Framework:** [Hugo](https://gohugo.io/) v0.164.0 (Extended Edition)\n",
                encoding="utf-8",
            )
            issues = validation.readme_version_issues(root)

        self.assertEqual(len(issues), 2)

    def test_theme_updates_are_written_to_package_manifest_and_lockfile(self):
        updates = [{"name": "prettier", "latest": "3.8.1"}]
        completed = {"returncode": 0, "stdout": "updated", "stderr": ""}
        with patch.object(maintenance, "npm_executable", return_value="npm"), patch.object(
            maintenance, "run", return_value=completed
        ) as run:
            result = maintenance.apply_theme_candidates(updates, Path("theme"))

        self.assertEqual(result, completed)
        run.assert_called_once_with(
            [
                "npm",
                "install",
                "--save-dev",
                "--package-lock-only",
                "--ignore-scripts",
                "prettier@3.8.1",
            ],
            cwd=Path("theme"),
            timeout=300,
        )


if __name__ == "__main__":
    unittest.main()
