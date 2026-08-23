# coding: utf-8
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "deploy" / "yukirin-server"


class DeploymentProfileTests(unittest.TestCase):
    def test_api_unit_reuses_host_interpreter_and_global_token_file(self):
        unit = (PROFILE / "systemd" / "user" / "capswriter-api.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("EnvironmentFile=%h/.config/capswriter/capswriter.env", unit)
        self.assertIn("EnvironmentFile=%h/.config/capswriter/api.env", unit)
        self.assertIn(".venv/bin/python-host", unit)
        self.assertIn("core_api.py", unit)
        self.assertNotIn("CAPSWRITER_TOKEN=", unit)

    def test_webui_profile_has_no_legacy_wrapper_dependency(self):
        webui_env = (PROFILE / "config" / "webui.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("CAPSWRITER_API_ENABLED=1", webui_env)
        self.assertNotIn("WRAPPER", webui_env)
        self.assertNotIn("9600", webui_env)

    def test_agent_wrapper_uses_upload_client_without_token_argument(self):
        wrapper = (PROFILE / "lxc" / "agents" / "caps-transcribe-api").read_text(
            encoding="utf-8"
        )
        self.assertIn("-m capswriter_plus.api.client", wrapper)
        self.assertIn("/etc/capswriter/capswriter.env", wrapper)
        self.assertNotIn("--token", wrapper)


if __name__ == "__main__":
    unittest.main()
