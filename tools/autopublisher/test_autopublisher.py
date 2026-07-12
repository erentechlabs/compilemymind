import json
import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
