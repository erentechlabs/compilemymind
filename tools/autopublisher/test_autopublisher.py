import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import autopublisher  # noqa: E402


class AutopublisherTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "publishing": {
                "min_words": 20,
                "required_source_count": 2,
                "require_table": True,
                "require_featured_image": True,
                "max_similarity": 0.42,
                "max_title_similarity": 0.55,
            },
            "taxonomy": {"allowed_categories": ["guide", "technology"]},
        }

    def test_slugify_is_stable_for_hugo_paths(self):
        self.assertEqual(
            autopublisher.slugify("DNS & HTTP: A Practical Guide"),
            "dns-and-http-a-practical-guide",
        )

    def test_safe_filename_preserves_the_file_extension(self):
        self.assertEqual(
            autopublisher.safe_filename("ghostcommit-attack-flow.svg", "diagram.svg"),
            "ghostcommit-attack-flow.svg",
        )
        self.assertEqual(
            autopublisher.safe_filename("../comparison-chart.png", "chart.svg"),
            "comparison-chart.png",
        )

    def test_model_json_parser_handles_markdown_fences(self):
        self.assertEqual(autopublisher.parse_model_json("```json\n{\"ok\": true}\n```"), {"ok": True})

    def test_empty_environment_model_overrides_keep_config_defaults(self):
        client = autopublisher.GeminiClient({"gemini": {}}, autopublisher.EventLog())
        self.assertEqual(client.text_model, "gemini-3.5-flash")
        self.assertEqual(client.image_model, "gemini-3.1-flash-image")

    def test_active_model_state_overrides_config_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            model_state_path = Path(directory) / "model-state.json"
            model_state_path.write_text(
                json.dumps(
                    {
                        "active_models": {
                            "text": "gemini-4.0-flash",
                            "qa": "gemini-4.0-flash",
                            "grounded": "gemini-4.0-flash",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(autopublisher, "MODEL_STATE_PATH", model_state_path), patch.dict(
                os.environ,
                {
                    "GEMINI_TEXT_MODEL": "",
                    "GEMINI_QA_MODEL": "",
                    "GEMINI_GROUNDED_RESEARCH_MODEL": "",
                },
                clear=False,
            ):
                client = autopublisher.GeminiClient(
                    {
                        "gemini": {
                            "text_model": "gemini-3.5-flash",
                            "qa_model": "gemini-3.5-flash",
                            "grounded_research_model": "gemini-3.5-flash",
                            "model_upgrade": {"enabled": True},
                        }
                    },
                    autopublisher.EventLog(),
                )
        self.assertEqual(client.text_model, "gemini-4.0-flash")
        self.assertEqual(client.qa_model, "gemini-4.0-flash")
        self.assertEqual(client.grounded_model, "gemini-4.0-flash")

    def test_untrusted_model_sources_are_not_published(self):
        research = [
            autopublisher.ResearchItem(
                source="Trusted",
                title="Trusted source",
                url="https://trusted.example/article",
                summary="summary",
                published="",
                categories=["technology"],
                score=1.0,
            ),
            autopublisher.ResearchItem(
                source="Trusted",
                title="Second source",
                url="https://trusted.example/second",
                summary="summary",
                published="",
                categories=["technology"],
                score=0.9,
            ),
        ]
        sources = autopublisher.supplement_article_sources(
            [
                {"title": "Invented", "url": "https://untrusted.example/fake"},
                {"title": "Trusted", "url": "https://trusted.example/article"},
            ],
            {"source_urls": ["https://untrusted.example/topic"]},
            research,
            self.config,
        )
        self.assertEqual([source["url"] for source in sources], [
            "https://trusted.example/article",
            "https://trusted.example/second",
        ])

    def test_ai_qa_failure_is_fail_closed(self):
        class BrokenClient:
            qa_model = "test"

            def generate_json(self, *args, **kwargs):
                raise RuntimeError("simulated outage")

        result = autopublisher.ai_qa(
            BrokenClient(),
            {"title": "Test", "description": "A" * 120, "categories": [], "tags": [], "sources": [], "article_markdown": "body"},
            {"title": "Test"},
            self.config,
            autopublisher.EventLog(),
        )
        self.assertFalse(result["approved"])
        self.assertEqual(result["score"], 0.0)

    def test_grounded_research_429_raises_quota_error(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
            patch.object(autopublisher, "http_request", return_value=(429, b"quota exceeded", {})):
            client = autopublisher.GeminiClient({"gemini": {}}, autopublisher.EventLog())
            with self.assertRaises(autopublisher.GeminiQuotaError):
                client.grounded_research("test")

    def test_publish_result_marker_records_explicit_result(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "publish-result.json"
            with patch.object(autopublisher, "PUBLISH_RESULT_PATH", marker):
                autopublisher.write_publish_result("rejected", reason="qa_failed")
            payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(payload["result"], "rejected")
        self.assertEqual(payload["reason"], "qa_failed")

    def test_maintenance_stops_cleanly_when_grounded_research_is_quota_limited(self):
        state = {"maintenance_reviews": {}, "last_runs": {}}
        post = SimpleNamespace(slug="test-post", title="Test post", date="2026-01-01", body="A readable article body.")

        class QuotaClient:
            text_model = "test"

            def require_key(self):
                return None

            def grounded_research(self, _prompt):
                raise autopublisher.GeminiQuotaError("quota")

            def generate_json(self, *_args, **_kwargs):
                raise AssertionError("maintenance must not generate after a quota response")

        with patch.object(autopublisher, "load_config", return_value={"maintenance": {"max_articles_per_run": 1}}), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=[post]), \
            patch.object(autopublisher, "select_posts_for_maintenance", return_value=[post]), \
            patch.object(autopublisher, "GeminiClient", return_value=QuotaClient()), \
            patch.object(autopublisher, "run_hugo_build", return_value=True), \
            patch.object(autopublisher, "save_state") as save_state:
            result = autopublisher.run_maintain(SimpleNamespace(max_articles=None, dry_run=False))

        self.assertEqual(result, 0)
        self.assertEqual(state["last_runs"]["maintain"]["result"], "quota_limited")
        save_state.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
