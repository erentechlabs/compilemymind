import unittest

from infra_maintenance import version_risk


class InfrastructureMaintenanceTests(unittest.TestCase):
    def test_equal_versions_are_unchanged(self):
        self.assertEqual(version_risk("0.163.1", "0.163.1"), "none")

    def test_hugo_patch_and_minor_updates_are_automatic_candidates(self):
        self.assertEqual(version_risk("0.163.1", "0.163.2"), "low")
        self.assertEqual(version_risk("0.163.1", "0.164.0"), "low")

    def test_major_updates_remain_manual_candidates(self):
        self.assertEqual(version_risk("0.164.0", "1.0.0"), "high")


if __name__ == "__main__":
    unittest.main()
