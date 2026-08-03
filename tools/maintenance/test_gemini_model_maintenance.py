import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gemini_model_maintenance as maintenance


def listed_model(name: str) -> dict[str, object]:
    return {
        "baseModelId": name,
        "modelStage": "STABLE",
        "supportedGenerationMethods": ["generateContent"],
        "inputTokenLimit": 100000,
        "outputTokenLimit": 32000,
    }


class GeminiModelMaintenanceTests(unittest.TestCase):
    def test_missing_key_is_reported_as_deferred_without_failure(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False), patch.object(
            sys, "argv", ["gemini_model_maintenance.py"]
        ), contextlib.redirect_stdout(output):
            status = maintenance.main()

        self.assertEqual(status, 0)
        self.assertIn('"status": "deferred"', output.getvalue())
        self.assertIn("::warning title=Gemini model maintenance deferred::", output.getvalue())

    def test_current_retired_model_is_replaced_after_smoke_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "model-state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gemini": {
                            "text_model": "gemini-3.5-flash",
                            "qa_model": "gemini-3.5-flash",
                            "grounded_research_model": "gemini-3.5-flash",
                            "model_upgrade": {
                                "enabled": True,
                                "stable_only": True,
                                "family": "flash",
                                "max_major_jump": 1,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            models = [listed_model("gemini-3.5-flash"), listed_model("gemini-3.6-flash")]
            with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}, clear=False), patch.object(
                sys, "argv", ["gemini_model_maintenance.py"]
            ), patch.object(maintenance, "CONFIG_PATH", config_path), patch.object(
                maintenance, "MODEL_STATE_PATH", state_path
            ), patch.object(maintenance, "list_models", return_value=models), patch.object(
                maintenance,
                "smoke_test",
                side_effect=[RuntimeError("HTTP 410 retired"), None],
            ):
                status = maintenance.main()

            self.assertEqual(status, 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["active_models"]["text"], "gemini-3.6-flash")
            self.assertIn("unavailable", state["reason"])

    def test_discovery_outage_defers_without_changing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "model-state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "gemini": {
                            "text_model": "gemini-3.5-flash",
                            "model_upgrade": {"enabled": True},
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}, clear=False), patch.object(
                sys, "argv", ["gemini_model_maintenance.py"]
            ), patch.object(maintenance, "CONFIG_PATH", config_path), patch.object(
                maintenance, "MODEL_STATE_PATH", state_path
            ), patch.object(
                maintenance, "list_models", side_effect=RuntimeError("HTTP 503")
            ), contextlib.redirect_stdout(output):
                status = maintenance.main()

            self.assertEqual(status, 0)
            self.assertFalse(state_path.exists())
            self.assertIn('"status": "deferred"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
