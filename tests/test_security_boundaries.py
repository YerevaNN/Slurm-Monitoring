import os
import unittest
from unittest.mock import patch

os.environ["SLURM_DASHBOARD_DISABLE_RECORDER"] = "1"
os.environ["SLURM_DASHBOARD_ADMIN_TOKEN"] = "test-admin-token"

import app as dashboard_app
import config
from slurm import build_job_settings_command


class CommandGenerationTests(unittest.TestCase):
    def test_builds_command_without_running_subprocess(self):
        with patch("slurm.subprocess.run") as run:
            ok, command = build_job_settings_command("217187_12", priority=1234)
        self.assertTrue(ok)
        self.assertEqual(command, "scontrol update jobid=217187_12 Priority=1234")
        run.assert_not_called()

    def test_command_endpoint_replaces_mutating_endpoint(self):
        client = dashboard_app.app.test_client()
        response = client.post(
            "/api/job/217187_12/settings-command",
            json={"priority": 1234},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["command"],
            "scontrol update jobid=217187_12 Priority=1234",
        )
        self.assertNotEqual(
            client.post("/api/job/217187_12/settings", json={"priority": 1234}).status_code,
            200,
        )


class MetadataAuthorizationTests(unittest.TestCase):
    def setUp(self):
        config.ADMIN_TOKEN = "test-admin-token"
        self.client = dashboard_app.app.test_client()

    @patch("app.history.create_unit", return_value=7)
    def test_metadata_write_rejects_missing_token(self, create_unit):
        response = self.client.post("/api/units", json={"title": "Example"})
        self.assertEqual(response.status_code, 401)
        create_unit.assert_not_called()

    @patch("app.history.create_unit", return_value=7)
    def test_metadata_write_accepts_admin_token(self, create_unit):
        response = self.client.post(
            "/api/units",
            json={"title": "Example", "color": "#123456"},
            headers={"X-Slurm-Monitor-Admin-Token": "test-admin-token"},
        )
        self.assertEqual(response.status_code, 200)
        create_unit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
