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

    def test_topic_selection_uses_github_models_when_token_is_available(self):
        response = {"choices": [{"message": {"content": '{"topics": []}'}}]}
        config = {
            "gemini": {"model_upgrade": {"enabled": False}},
            "github_models": {
                "enabled": True,
                "model": "openai/gpt-4o-mini",
                "lightweight_tasks": ["topic_selection"],
                "max_output_tokens": 4096,
            },
        }
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GITHUB_MODELS_TOKEN": "github-token"},
            clear=False,
        ), patch.object(autopublisher, "http_request", return_value=(200, json.dumps(response).encode(), {})) as request:
            client = autopublisher.GeminiClient(config, autopublisher.EventLog())
            result = client.generate_json("Choose a topic", task="topic_selection")

        self.assertEqual(result, {"topics": []})
        self.assertIn("models.github.ai/inference/chat/completions", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer github-token")
        self.assertEqual(request.call_args.kwargs["payload"]["response_format"], {"type": "json_object"})

    def test_github_models_falls_back_to_gemini_when_rate_limited(self):
        github_response = (429, b"rate limited", {})
        gemini_response = (
            200,
            json.dumps({"candidates": [{"content": {"parts": [{"text": '{"topics": []}'}]}}]}).encode(),
            {},
        )
        config = {
            "gemini": {"model_upgrade": {"enabled": False}},
            "github_models": {
                "enabled": True,
                "model": "openai/gpt-4o-mini",
                "lightweight_tasks": ["topic_selection"],
            },
        }
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GITHUB_MODELS_TOKEN": "github-token"},
            clear=False,
        ), patch.object(autopublisher, "http_request", side_effect=[github_response, gemini_response]):
            client = autopublisher.GeminiClient(config, autopublisher.EventLog())
            result = client.generate_json("Choose a topic", task="topic_selection")

        self.assertEqual(result, {"topics": []})

    def test_github_models_falls_back_when_json_output_is_malformed(self):
        github_response = (
            200,
            json.dumps({"choices": [{"message": {"content": "not valid json"}}]}).encode(),
            {},
        )
        gemini_response = (
            200,
            json.dumps({"candidates": [{"content": {"parts": [{"text": '{"topics": []}'}]}}]}).encode(),
            {},
        )
        config = {
            "gemini": {"model_upgrade": {"enabled": False}},
            "github_models": {
                "enabled": True,
                "model": "openai/gpt-4o-mini",
                "lightweight_tasks": ["topic_selection"],
            },
        }
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-key", "GITHUB_MODELS_TOKEN": "github-token"},
            clear=False,
        ), patch.object(autopublisher, "http_request", side_effect=[github_response, gemini_response]):
            client = autopublisher.GeminiClient(config, autopublisher.EventLog())
            result = client.generate_json("Choose a topic", task="topic_selection")

        self.assertEqual(result, {"topics": []})

    def test_metadata_enrichment_uses_lightweight_task(self):
        calls = []

        class MetadataClient:
            def generate_json(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {
                    "description": "A practical guide to designing reliable distributed systems with clear trade-offs, examples, and implementation advice for software teams.",
                    "summary": "A practical distributed-systems guide.",
                    "categories": ["software-engineering"],
                    "tags": ["distributed-systems", "architecture"],
                }

        config = {
            "taxonomy": {"allowed_categories": ["guide", "software-engineering"]},
        }
        article = {
            "title": "Distributed Systems Design",
            "description": "",
            "categories": ["guide"],
            "tags": ["systems"],
            "article_markdown": "A detailed article about distributed systems.",
        }
        autopublisher.enrich_article_metadata(
            MetadataClient(),
            article,
            {"title": article["title"], "categories": ["guide"]},
            config,
            autopublisher.EventLog(),
        )

        self.assertEqual(calls[0][1]["task"], "metadata_enrichment")
        self.assertEqual(article["categories"], ["guide", "software-engineering"])
        self.assertEqual(article["tags"], ["distributed-systems", "architecture"])
        self.assertIn("summary", article)

    def test_markdown_format_issues_reject_structural_errors(self):
        issues = autopublisher.markdown_format_issues(
            "# Duplicate title\n\n##\n\n```python\nprint('hello')\n"
        )
        self.assertTrue(any("top-level H1" in issue for issue in issues))
        self.assertTrue(any("empty Markdown heading" in issue for issue in issues))
        self.assertTrue(any("code fences are unbalanced" in issue for issue in issues))

    def test_svg_text_collision_check_rejects_overlapping_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlap.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<text x="100" y="100" font-size="20">First label</text>'
                '<text x="100" y="100" font-size="20">Second label</text>'
                '</svg>',
                encoding="utf-8",
            )
            with patch.object(autopublisher, "ROOT", Path(directory)):
                issues = autopublisher.svg_text_overlap_issues(path)
        self.assertTrue(issues)

    def test_flowchart_combines_multiple_labels_on_one_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flow.svg"
            autopublisher.render_flowchart_svg(
                {
                    "title": "Flow",
                    "nodes": [
                        {"id": "controller", "label": "Controller"},
                        {"id": "analysis", "label": "Analysis"},
                    ],
                    "edges": [
                        {"from": "controller", "to": "analysis", "label": "Watch Pod Events"},
                        {"from": "controller", "to": "analysis", "label": "Inspect Image & Args"},
                    ],
                },
                path,
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("Watch Pod Events • Inspect Image &amp;", content)
            with patch.object(autopublisher, "ROOT", Path(directory)):
                self.assertEqual(autopublisher.svg_text_overlap_issues(path), [])

    def test_topic_selection_prompt_is_compact_for_lightweight_models(self):
        research = [
            autopublisher.ResearchItem(
                source=f"Source {index}",
                title=f"Current technical update {index}",
                url=f"https://example.com/{index}",
                summary="summary " * 200,
                published="2026-07-13",
                categories=["software-engineering"],
                score=1.0,
                snippet="snippet " * 300,
            )
            for index in range(36)
        ]
        posts = [
            autopublisher.Post(
                path=Path(f"post-{index}/index.md"),
                slug=f"post-{index}",
                title=f"Existing post {index}",
                description="description " * 100,
                date="2026-01-01",
                tags=["software-engineering"],
                categories=["software-engineering"],
                body="body",
                frontmatter={},
            )
            for index in range(60)
        ]
        config = {
            "site": {"timezone": "Europe/Istanbul"},
            "research": {},
            "taxonomy": {
                "allowed_categories": ["guide", "software-engineering"],
                "balance_categories": ["software-engineering"],
            },
        }
        prompt = autopublisher.topic_selection_prompt(research, None, posts, config)
        self.assertLess(len(prompt), 24000)

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

    def test_generate_content_503_raises_transient_error(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
            patch.object(autopublisher, "http_request", return_value=(503, b"temporarily overloaded", {})):
            client = autopublisher.GeminiClient({"gemini": {}}, autopublisher.EventLog())
            with self.assertRaises(autopublisher.GeminiTransientError):
                client.generate_json("test")

    def test_grounded_research_fallback_is_enabled_by_default(self):
        self.assertTrue(autopublisher.grounded_research_fallback_enabled({"gemini": {}}))
        self.assertFalse(
            autopublisher.grounded_research_fallback_enabled(
                {"gemini": {"grounded_research_fallback_to_feeds": False}}
            )
        )

    def test_publish_continues_from_rss_when_grounding_is_quota_limited(self):
        config = {
            "gemini": {
                "enable_google_search_grounding": True,
                "grounded_research_fallback_to_feeds": True,
            },
            "publishing": {"max_regeneration_attempts": 0},
            "site": {"content_dir": "content/posts"},
        }
        state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}}
        research = [
            autopublisher.ResearchItem(
                source="Trusted",
                title="A current software engineering update",
                url="https://trusted.example/update",
                summary="A useful summary",
                published="",
                categories=["software-engineering"],
                score=2.0,
            )
        ]
        topic = {
            "title": "A current software engineering guide",
            "slug": "a-current-software-engineering-guide",
            "categories": ["software-engineering"],
            "tags": ["software"],
            "source_urls": [research[0].url],
        }
        article = {
            "title": topic["title"],
            "slug": topic["slug"],
            "description": "A useful guide.",
            "categories": topic["categories"],
            "tags": topic["tags"],
            "sources": [{"title": research[0].title, "url": research[0].url}],
            "article_markdown": "Article body",
        }

        class GroundingQuotaClient:
            qa_model = "test"

            def require_key(self):
                return None

            def grounded_research(self, _prompt):
                raise autopublisher.GeminiQuotaError("grounding quota")

            def generate_json(self, _prompt, **_kwargs):
                return {}

        with patch.object(autopublisher, "load_config", return_value=config), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=[]), \
            patch.object(autopublisher, "collect_research", return_value=research), \
            patch.object(autopublisher, "enrich_research_snippets"), \
            patch.object(autopublisher, "GeminiClient", return_value=GroundingQuotaClient()), \
            patch.object(autopublisher, "choose_topic", return_value=topic) as choose_topic, \
            patch.object(autopublisher, "normalize_article_payload", return_value=article), \
            patch.object(autopublisher, "deterministic_qa", return_value=[]), \
            patch.object(autopublisher, "ai_qa", return_value={"approved": True, "score": 0.9}), \
            patch.object(autopublisher, "write_article_bundle", return_value=autopublisher.ROOT / "content/posts/test/index.md"), \
            patch.object(autopublisher, "save_state"), \
            patch.object(autopublisher, "write_publish_result"):
            result = autopublisher.run_publish(SimpleNamespace(dry_run=True))

        self.assertEqual(result, 0)
        self.assertIsNone(choose_topic.call_args.args[2])
        self.assertEqual(state["last_runs"]["publish"]["result"], "dry_run")

    def test_publish_records_retryable_when_generation_is_temporarily_unavailable(self):
        config = {
            "gemini": {"enable_google_search_grounding": False},
            "publishing": {"max_regeneration_attempts": 0},
            "site": {"content_dir": "content/posts"},
        }
        state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}}
        research = [
            autopublisher.ResearchItem(
                source="Trusted",
                title="A current software engineering update",
                url="https://trusted.example/update",
                summary="A useful summary",
                published="",
                categories=["software-engineering"],
                score=2.0,
            )
        ]
        topic = {"title": "A current software engineering guide", "slug": "a-current-guide"}

        class TransientClient:
            def require_key(self):
                return None

            def generate_json(self, _prompt, **_kwargs):
                raise autopublisher.GeminiTransientError("model unavailable")

        with patch.object(autopublisher, "load_config", return_value=config), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=[]), \
            patch.object(autopublisher, "collect_research", return_value=research), \
            patch.object(autopublisher, "enrich_research_snippets"), \
            patch.object(autopublisher, "GeminiClient", return_value=TransientClient()), \
            patch.object(autopublisher, "choose_topic", return_value=topic), \
            patch.object(autopublisher, "save_state"), \
            patch.object(autopublisher, "write_publish_result") as write_result:
            result = autopublisher.run_publish(SimpleNamespace(dry_run=False))

        self.assertEqual(result, 0)
        self.assertEqual(state["last_runs"]["publish"]["result"], "retryable")
        self.assertEqual(write_result.call_args_list[-1].args, ("retryable",))
        self.assertEqual(
            write_result.call_args_list[-1].kwargs,
            {"stage": "article_generation", "error": "model unavailable"},
        )

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
