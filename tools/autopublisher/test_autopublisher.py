import json
import gzip
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import autopublisher  # noqa: E402


class AutopublisherTests(unittest.TestCase):
    def test_production_model_fallback_covers_publication_critical_tasks(self):
        config = autopublisher.load_config()
        github_models = config["github_models"]
        self.assertEqual(github_models["model"], "openai/gpt-4.1")
        self.assertIn("openai/gpt-4.1-mini", github_models["fallback_models"])
        self.assertTrue({"article_generation", "quality_assurance"}.issubset(github_models["lightweight_tasks"]))
        self.assertGreaterEqual(github_models["max_output_tokens"], 12000)
        self.assertTrue(config["publishing"]["prefer_evergreen_after_quota"])
        self.assertTrue(config["publishing"]["prefer_source_qualified_evergreen_first"])
        self.assertEqual(config["publishing"]["max_topic_attempts"], 1)
        self.assertTrue(config["cost_control"]["require_source_qualified_topic"])
        self.assertEqual(config["cost_control"]["max_topic_selection_calls_per_run"], 1)

    def test_publisher_code_pushes_run_tests_without_triggering_paid_publication(self):
        publisher_workflow = (autopublisher.ROOT / ".github/workflows/autonomous-publish.yml").read_text(encoding="utf-8")
        trigger_block = publisher_workflow.split("concurrency:", 1)[0]
        deploy_workflow = (autopublisher.ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        self.assertIn("  push:", trigger_block)
        self.assertIn('".autopublisher/publish-now"', trigger_block)
        self.assertNotIn('"tools/autopublisher/**"', trigger_block)
        self.assertIn("schedule:", trigger_block)
        self.assertIn("workflow_dispatch:", trigger_block)
        self.assertIn("python -m unittest discover -s tools/autopublisher", deploy_workflow)

    def test_production_has_three_source_qualified_evergreen_fallbacks(self):
        config = autopublisher.load_config()
        existing_slugs = {post.slug for post in autopublisher.load_posts(config)}
        configured_fallbacks = [
            topic
            for topic in config["research"]["evergreen_topics"]
            if topic.get("offline_fallback")
        ]
        remaining = [
            topic
            for topic in configured_fallbacks
            if topic.get("slug") not in existing_slugs
        ]
        self.assertIsInstance(remaining, list)
        self.assertGreaterEqual(len(configured_fallbacks), 4)
        self.assertTrue(remaining, "At least one unpublished offline fallback must remain available.")
        required = config["publishing"]["required_source_count"]
        for topic in configured_fallbacks:
            source_urls = {source["url"] for source in topic.get("seed_sources", [])}
            self.assertGreaterEqual(len(source_urls), required, topic.get("slug"))

    def test_configured_offline_evergreen_fallbacks_pass_quality_gates(self):
        config = autopublisher.load_config()
        posts = autopublisher.load_posts(config)
        for configured in config["research"]["evergreen_topics"]:
            if not configured.get("offline_fallback") or configured.get("slug") in {post.slug for post in posts}:
                continue
            topic = dict(configured)
            topic["source_urls"] = [item["url"] for item in topic["seed_sources"]]
            sources = [
                autopublisher.ResearchItem(
                    "Official", item["title"], item["url"],
                    f"{item['title']} official documentation for {topic['title']}.", "",
                    topic["categories"], 2.0, item["title"], True,
                )
                for item in topic["seed_sources"]
            ]
            article, qa, feedback = autopublisher.deterministic_evergreen_fallback(
                topic, sources, posts, config, autopublisher.EventLog()
            )
            self.assertTrue(article, f"{topic['slug']}: {feedback}")
            self.assertTrue(qa["approved"])
            self.assertGreaterEqual(qa["quality"]["score"], config["publishing"]["quality_min_score"])

    def test_publish_uses_configured_offline_fallback_without_model_generation(self):
        config = autopublisher.load_config()
        posts = autopublisher.load_posts(config)
        topic = dict(next(
            item for item in config["research"]["evergreen_topics"]
            if item.get("offline_fallback") and item.get("slug") not in {post.slug for post in posts}
        ))
        topic["source_urls"] = [item["url"] for item in topic["seed_sources"]]
        sources = [
            autopublisher.ResearchItem(
                "Official", item["title"], item["url"],
                f"{item['title']} official documentation for {topic['title']}.", "",
                topic["categories"], 2.0, item["title"], True,
            )
            for item in topic["seed_sources"]
        ]
        state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}}

        class NoModelClient:
            def require_key(self):
                return None

            def generate_json(self, *_args, **_kwargs):
                raise AssertionError("configured offline fallback must not call a model")

        with patch.object(autopublisher, "load_config", return_value=config), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=posts), \
            patch.object(autopublisher, "collect_research", return_value=sources), \
            patch.object(autopublisher, "validate_research_items", return_value=sources), \
            patch.object(autopublisher, "GeminiClient", return_value=NoModelClient()), \
            patch.object(autopublisher, "choose_evergreen_topic", return_value=topic), \
            patch.object(autopublisher, "collect_topic_research", return_value=sources), \
            patch.object(autopublisher, "write_article_bundle", return_value=autopublisher.ROOT / f"content/posts/{topic['slug']}/index.md"), \
            patch.object(autopublisher, "save_state"), \
            patch.object(autopublisher, "write_publish_result"):
            result = autopublisher.run_publish(SimpleNamespace(dry_run=True))

        self.assertEqual(result, 0)
        self.assertEqual(state["last_runs"]["publish"]["result"], "dry_run")
        self.assertEqual(state["generated_posts"][-1]["slug"], topic["slug"])

    def test_production_evergreen_selection_skips_published_offline_fallbacks(self):
        config = autopublisher.load_config()
        posts = autopublisher.load_posts(config)
        selected = autopublisher.choose_evergreen_topic(posts, config, autopublisher.EventLog())
        self.assertIsNotNone(selected)
        self.assertNotEqual(selected["slug"], "troubleshoot-microsoft-entra-sign-in-errors")
        self.assertNotIn(selected["slug"], {post.slug for post in posts})

    def test_offline_evergreen_recovery_passes_quality_gates(self):
        config = autopublisher.load_config()
        topic = dict(next(item for item in config["research"]["evergreen_topics"] if item["slug"] == "troubleshooting-windows-event-logs-powershell"))
        topic["source_urls"] = [item["url"] for item in topic["seed_sources"]]
        sources = [
            autopublisher.ResearchItem(
                "Microsoft", item["title"], item["url"], item["title"], "",
                ["system-administration"], 2.0, item["title"], True,
            )
            for item in topic["seed_sources"]
        ]
        article, qa, feedback = autopublisher.deterministic_evergreen_fallback(
            topic,
            sources,
            [post for post in autopublisher.load_posts(config) if post.slug != topic["slug"]],
            config,
            autopublisher.EventLog(),
        )
        self.assertTrue(article, feedback)
        self.assertTrue(qa["approved"])
        self.assertGreaterEqual(qa["quality"]["score"], config["publishing"]["quality_min_score"])

    def test_dns_and_kubernetes_offline_recovery_pass_quality_gates(self):
        config = autopublisher.load_config()
        posts = autopublisher.load_posts(config)
        for slug in ("troubleshooting-windows-dns-powershell", "kubernetes-probe-misconfigurations-fixes"):
            topic = dict(next(item for item in config["research"]["evergreen_topics"] if item["slug"] == slug))
            topic["source_urls"] = [item["url"] for item in topic["seed_sources"]]
            sources = [
                autopublisher.ResearchItem(
                    "Official", item["title"], item["url"],
                    f"{item['title']} official documentation covers configuration and troubleshooting.", "",
                    topic["categories"], 2.0, item["title"], True,
                )
                for item in topic["seed_sources"]
            ]
            article, qa, feedback = autopublisher.deterministic_evergreen_fallback(
                topic, sources, [post for post in posts if post.slug != slug], config, autopublisher.EventLog()
            )
            self.assertTrue(article, f"{slug}: {feedback}")
            self.assertTrue(qa["approved"])

    def test_evergreen_first_publish_path_skips_paid_discovery_calls(self):
        config = {
            "gemini": {"enable_google_search_grounding": True},
            "publishing": {
                "required_source_count": 3,
                "prefer_source_qualified_evergreen_first": True,
                "max_topic_attempts": 1,
                "max_evergreen_topic_attempts": 0,
            },
            "cost_control": {"max_topic_selection_calls_per_run": 1},
            "site": {"content_dir": "content/posts"},
        }
        state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}}
        sources = [
            autopublisher.ResearchItem(
                "Official", f"Source {index}", f"https://example.com/{index}",
                "Official troubleshooting documentation", "", ["system-administration"], 2.0,
            )
            for index in range(3)
        ]
        topic = {
            "title": "Troubleshooting Windows Event Logs with PowerShell",
            "slug": "troubleshooting-windows-event-logs-powershell",
            "categories": ["system-administration"],
            "tags": ["powershell"],
        }
        article = {
            **topic,
            "description": "Troubleshoot Windows Event Logs safely.",
            "sources": [{"title": item.title, "url": item.url} for item in sources],
            "article_markdown": "## Steps\n\nValidated guidance.",
        }

        class NoDiscoveryClient:
            def require_key(self):
                return None

            def grounded_research(self, _prompt):
                raise AssertionError("paid discovery must not run before a source-qualified evergreen topic")

        with patch.object(autopublisher, "load_config", return_value=config), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=[]), \
            patch.object(autopublisher, "collect_research", return_value=sources), \
            patch.object(autopublisher, "enrich_research_snippets"), \
            patch.object(autopublisher, "GeminiClient", return_value=NoDiscoveryClient()), \
            patch.object(autopublisher, "choose_evergreen_topic", return_value=topic), \
            patch.object(autopublisher, "choose_topic") as choose_topic, \
            patch.object(autopublisher, "collect_topic_research", return_value=sources), \
            patch.object(autopublisher, "generate_approved_article", return_value=(article, {"approved": True}, "")), \
            patch.object(autopublisher, "write_article_bundle", return_value=autopublisher.ROOT / "content/posts/test/index.md"), \
            patch.object(autopublisher, "save_state"), \
            patch.object(autopublisher, "write_publish_result"):
            result = autopublisher.run_publish(SimpleNamespace(dry_run=True))

        self.assertEqual(result, 0)
        choose_topic.assert_not_called()
        self.assertEqual(state["last_runs"]["publish"]["result"], "dry_run")

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

    def test_hugo_shortcode_delimiters_are_escaped_only_in_prose(self):
        markdown = """## Example\n\nUse {{< notice >}} in prose.\n\n```yaml\nvalue: {{ .Values.port }}\n```\n"""
        normalized = autopublisher.remove_accidental_frontmatter(markdown)
        self.assertIn("&#123;&#123;&lt; notice >}}", normalized)
        self.assertIn("value: {{ .Values.port }}", normalized)

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

    def test_invalid_gemini_json_is_classified_as_retryable(self):
        malformed_response = (
            200,
            json.dumps({"candidates": [{"content": {"parts": [{"text": '{"topics": [{"title": "cut off"'}]}}]}).encode(),
            {},
        )
        config = {
            "gemini": {"model_upgrade": {"enabled": False}},
            "github_models": {"enabled": False},
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=False), \
            patch.object(autopublisher, "http_request", return_value=malformed_response):
            client = autopublisher.GeminiClient(config, autopublisher.EventLog())
            with self.assertRaises(autopublisher.GeminiTransientError):
                client.generate_json("Choose a topic", task="topic_selection")

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

    def test_markdown_heading_checks_ignore_fenced_code_comments(self):
        markdown = "# Introduction\n\n```python\n# This is a code comment\nprint('hello')\n```\n\n## Details"
        normalized = autopublisher.remove_accidental_frontmatter(markdown)
        self.assertIn("## Introduction", normalized)
        self.assertIn("# This is a code comment", normalized)
        self.assertFalse(any("top-level H1" in issue for issue in autopublisher.markdown_format_issues(normalized)))

    def test_http_body_decoder_handles_gzip_feeds(self):
        payload = b"<?xml version='1.0'?><feed />"
        self.assertEqual(
            autopublisher.decode_http_body(gzip.compress(payload), {"content-encoding": "gzip"}),
            payload,
        )

    def test_qa_feedback_formats_structured_model_items(self):
        self.assertEqual(
            autopublisher.feedback_text(
                [{"issue": "Missing table", "required_fix": "Add a comparison table"}, "Retry"],
                "fallback",
            ),
            "Missing table; Add a comparison table\nRetry",
        )

    def test_generation_feedback_preserves_rejected_draft_for_repair(self):
        feedback = autopublisher.generation_feedback(
            ["Article is too short: 40 words, expected at least 1400.", "Article should include a useful Markdown table."],
            {"article_markdown": "## Existing explanation\n\nKeep this useful detail."},
        )
        self.assertIn("complete replacement article", feedback)
        self.assertIn("## Existing explanation", feedback)

    def test_generation_feedback_discards_contaminated_draft(self):
        feedback = autopublisher.generation_feedback(
            ["Claim is not directly supported by its referenced source text: unrelated claim"],
            {"article_markdown": "## Contaminated\n\nAn unrelated Headlamp claim."},
        )
        self.assertIn("Discard the previous draft completely", feedback)
        self.assertNotIn("unrelated Headlamp claim", feedback)

    def test_topic_research_does_not_pad_with_unrelated_feed_items(self):
        topic = {
            "title": "Building a custom metrics exporter for Kubernetes",
            "search_intent": "Create and test a Kubernetes metrics exporter for Prometheus",
            "categories": ["developer-it-tools"],
            "tags": ["kubernetes", "monitoring"],
            "source_urls": ["https://kubernetes.io/blog/custom-metrics-exporter"],
        }
        selected = autopublisher.ResearchItem(
            "Kubernetes Blog", "Building a custom metrics exporter for Kubernetes",
            "https://kubernetes.io/blog/custom-metrics-exporter", "Exporter implementation details", "",
            ["developer-it-tools"], 2.0,
        )
        related = autopublisher.ResearchItem(
            "Kubernetes Docs", "Kubernetes custom metrics API and Prometheus adapters",
            "https://kubernetes.io/docs/custom-metrics", "Configure a custom metrics API for Prometheus", "",
            ["developer-it-tools"], 1.5,
        )
        unrelated = autopublisher.ResearchItem(
            "Kubernetes Blog", "Kubernetes Dashboard migration to Headlamp",
            "https://kubernetes.io/blog/headlamp", "Use Headlamp as a desktop cluster dashboard", "",
            ["developer-it-tools"], 2.2,
        )
        scoped = autopublisher.research_items_for_topic(
            topic,
            [selected, unrelated, related],
            limit=6,
            config={"research": {"topic_source_min_similarity": 0.16}},
        )
        self.assertIn(selected, scoped)
        self.assertIn(related, scoped)
        self.assertNotIn(unrelated, scoped)

    def test_collect_topic_research_validates_focused_grounded_sources(self):
        topic = {
            "title": "Configure Kubernetes custom metrics for Prometheus",
            "search_intent": "Configure a Kubernetes metrics adapter",
            "categories": ["developer-it-tools"],
            "tags": ["kubernetes", "monitoring"],
            "source_urls": ["https://kubernetes.io/docs/custom-metrics"],
        }
        initial = autopublisher.ResearchItem(
            "Kubernetes Docs", "Kubernetes custom metrics",
            "https://kubernetes.io/docs/custom-metrics", "Custom metrics API", "",
            ["developer-it-tools"], 2.0, validated=True,
        )

        class GroundedClient:
            def grounded_research(self, _prompt):
                return {
                    "text": "Focused documentation",
                    "citations": [
                        {"title": "Prometheus Adapter configuration", "url": "https://github.com/kubernetes-sigs/prometheus-adapter"},
                        {"title": "Kubernetes metrics APIs", "url": "https://kubernetes.io/docs/tasks/debug/metrics-resource-metrics-pipeline/"},
                    ],
                }

        def validate(items, _config, _log):
            for item in items:
                item.validated = True
                item.snippet = f"{topic['title']} {item.title} configuration guidance"
            return items

        config = {
            "gemini": {"enable_google_search_grounding": True},
            "publishing": {"required_source_count": 3},
            "research": {"topic_source_min_similarity": 0.1, "topic_source_max_items": 6},
        }
        with patch.object(autopublisher, "validate_research_items", side_effect=validate):
            scoped = autopublisher.collect_topic_research(
                GroundedClient(), topic, [initial], config, autopublisher.EventLog()
            )
        self.assertEqual(len(scoped), 3)
        self.assertTrue(all(item.validated for item in scoped))

    def test_collect_topic_research_validates_configured_evergreen_sources(self):
        topic = {
            "title": "Audit workflow token permissions",
            "categories": ["developer-it-tools"],
            "source_urls": [
                "https://docs.github.com/actions/token-one",
                "https://docs.github.com/actions/token-two",
                "https://docs.github.com/actions/token-three",
            ],
            "seed_sources": [
                {"title": "Token one", "url": "https://docs.github.com/actions/token-one"},
                {"title": "Token two", "url": "https://docs.github.com/actions/token-two"},
                {"title": "Token three", "url": "https://docs.github.com/actions/token-three"},
            ],
        }

        def validate(items, _config, _log):
            for item in items:
                item.validated = True
                item.snippet = f"{topic['title']} {item.title}"
            return items

        config = {
            "gemini": {"enable_google_search_grounding": False},
            "publishing": {"required_source_count": 3},
            "research": {"topic_source_min_similarity": 0.1, "topic_source_max_items": 6},
        }
        with patch.object(autopublisher, "validate_research_items", side_effect=validate):
            scoped = autopublisher.collect_topic_research(
                SimpleNamespace(), topic, [], config, autopublisher.EventLog()
            )
        self.assertEqual([item.url for item in scoped], topic["source_urls"])

    def test_choose_evergreen_topic_skips_existing_slug(self):
        existing = autopublisher.Post(
            path=Path("existing/index.md"),
            slug="existing-evergreen",
            title="Existing evergreen",
            description="Existing evergreen article",
            date="2026-01-01",
            tags=["github"],
            categories=["developer-it-tools"],
            body="## Existing\n\nExisting evergreen content.",
            frontmatter={},
        )
        config = {
            "publishing": {"topic_relevance_min_score": 0.0, "max_similarity": 1.0, "max_title_similarity": 1.0},
            "topic_scope": {"approved_categories": ["developer-it-tools"], "category_keywords": {}},
            "taxonomy": {"allowed_categories": ["developer-it-tools"], "controlled_tags": ["github"]},
            "research": {
                "evergreen_topics": [
                    {"title": "Existing evergreen", "slug": "existing-evergreen", "categories": ["developer-it-tools"]},
                    {"title": "Fresh evergreen", "slug": "fresh-evergreen", "categories": ["developer-it-tools"], "tags": ["github"]},
                ]
            },
        }
        selected = autopublisher.choose_evergreen_topic([existing], config, autopublisher.EventLog())
        self.assertEqual(selected["slug"], "fresh-evergreen")

    def test_evergreen_supporting_intent_is_not_rejected_by_category_overlap(self):
        existing = autopublisher.Post(
            Path("dns/index.md"),
            "dns-explained-how-your-browser-finds-a-website",
            "DNS Explained: How Your Browser Finds a Website",
            "A foundational explanation of recursive DNS resolution.",
            "2026-01-01",
            ["dns", "networking"],
            ["networking"],
            "## How DNS works\n\nBrowsers resolve names through recursive DNS.",
            {},
        )
        config = {
            "publishing": {
                "topic_relevance_min_score": 0.0,
                "max_title_similarity": 0.48,
                "max_search_intent_similarity": 0.72,
            },
            "topic_scope": {
                "approved_categories": ["system-administration", "networking"],
                "category_keywords": {"networking": ["dns", "network"], "system-administration": ["powershell"]},
            },
            "taxonomy": {
                "allowed_categories": ["system-administration", "networking"],
                "controlled_tags": ["powershell", "dns", "troubleshooting", "networking"],
            },
            "research": {
                "evergreen_topics": [{
                    "title": "Troubleshooting DNS on Windows with PowerShell",
                    "slug": "troubleshooting-windows-dns-powershell",
                    "primary_category": "system-administration",
                    "categories": ["system-administration", "networking"],
                    "tags": ["powershell", "dns", "troubleshooting", "networking"],
                    "search_intent": "Diagnose Windows DNS and network configuration problems with safe PowerShell commands.",
                    "seed_sources": [
                        {"title": "Resolve-DnsName", "url": "https://learn.microsoft.com/resolve-dnsname"},
                        {"title": "Test-NetConnection", "url": "https://learn.microsoft.com/test-netconnection"},
                        {"title": "Get-NetIPConfiguration", "url": "https://learn.microsoft.com/get-netipconfiguration"},
                    ],
                }],
            },
        }
        selected = autopublisher.choose_evergreen_topic([existing], config, autopublisher.EventLog())
        self.assertEqual(selected["slug"], "troubleshooting-windows-dns-powershell")

    def test_choose_topic_skips_candidate_without_prevalidated_source_bundle(self):
        sources = [
            autopublisher.ResearchItem(
                "Official", f"DNS command {index}", f"https://example.com/dns-{index}",
                "DNS troubleshooting command documentation", "", ["networking"], 2.0, validated=True,
            )
            for index in range(1, 3)
        ]

        class TopicClient:
            def generate_json(self, *_args, **_kwargs):
                return {"topics": [
                    {
                        "title": "Source-poor DNS topic",
                        "slug": "source-poor-dns-topic",
                        "primary_category": "networking",
                        "categories": ["networking"],
                        "search_intent": "Troubleshoot DNS",
                        "source_urls": [sources[0].url],
                    },
                    {
                        "title": "Troubleshooting DNS Commands",
                        "slug": "troubleshooting-dns-commands",
                        "primary_category": "networking",
                        "categories": ["networking"],
                        "search_intent": "Troubleshoot DNS with documented commands",
                        "source_urls": [source.url for source in sources],
                    },
                ]}

        config = {
            "publishing": {"required_source_count": 2, "topic_relevance_min_score": 0.0},
            "cost_control": {"require_source_qualified_topic": True},
            "source_validation": {"trusted_domains": ["example.com"]},
            "topic_scope": {"approved_categories": ["networking"], "category_keywords": {"networking": ["dns"]}},
            "taxonomy": {"allowed_categories": ["networking"], "controlled_tags": ["dns"]},
        }
        selected = autopublisher.choose_topic(
            TopicClient(), sources, None, [], config, autopublisher.EventLog()
        )
        self.assertEqual(selected["slug"], "troubleshooting-dns-commands")

    def test_choose_topic_rejects_selected_sources_that_do_not_match_the_topic(self):
        relevant_sources = [
            autopublisher.ResearchItem(
                "Official", title, f"https://example.com/relevant-{index}",
                "Serverless cloud function security and public exposure guidance.", "",
                ["cybersecurity"], 2.0, validated=True,
            )
            for index, title in enumerate([
                "Serverless cloud functions authentication guidance",
                "Serverless cloud functions public ingress guidance",
                "Serverless cloud functions security monitoring guidance",
            ], start=1)
        ]
        unrelated_sources = [
            autopublisher.ResearchItem(
                "Official", title, f"https://example.com/unrelated-{index}",
                summary, "", ["cybersecurity"], 2.0, validated=True,
            )
            for index, (title, summary) in enumerate([
                ("BigQuery enterprise features", "BigQuery analytics platform announcement."),
                ("IDC cloud market report", "Cloud market analyst report."),
            ], start=1)
        ]
        sources = [*relevant_sources, *unrelated_sources]

        class TopicClient:
            def generate_json(self, *_args, **_kwargs):
                return {"topics": [
                    {
                        "title": "Securing Serverless Cloud Functions from Public Exposure",
                        "slug": "polluted-serverless-source-bundle",
                        "primary_category": "cybersecurity",
                        "categories": ["cybersecurity"],
                        "tags": ["cybersecurity"],
                        "search_intent": "Secure serverless cloud functions from public exposure",
                        "source_urls": [relevant_sources[0].url, *[item.url for item in unrelated_sources]],
                    },
                    {
                        "title": "Securing Serverless Cloud Function Endpoints",
                        "slug": "secure-serverless-cloud-function-endpoints",
                        "primary_category": "cybersecurity",
                        "categories": ["cybersecurity"],
                        "tags": ["cybersecurity"],
                        "search_intent": "Secure serverless cloud functions from public exposure",
                        "source_urls": [item.url for item in relevant_sources],
                    },
                ]}

        config = {
            "publishing": {"required_source_count": 3, "topic_relevance_min_score": 0.0},
            "cost_control": {"require_source_qualified_topic": True},
            "source_validation": {"trusted_domains": ["example.com"]},
            "research": {"topic_source_min_similarity": 0.16, "topic_source_min_token_overlap": 2},
            "topic_scope": {"approved_categories": ["cybersecurity"], "category_keywords": {"cybersecurity": ["security"]}},
            "taxonomy": {"allowed_categories": ["cybersecurity"], "controlled_tags": ["cybersecurity"]},
        }
        selected = autopublisher.choose_topic(TopicClient(), sources, None, [], config, autopublisher.EventLog())
        self.assertEqual(selected["slug"], "secure-serverless-cloud-function-endpoints")

    def test_generate_approved_article_retries_with_repair_context(self):
        prompts = []

        class ArticleClient:
            def generate_json(self, prompt, **kwargs):
                prompts.append((prompt, kwargs))
                return {}

        topic = {"title": "A reliable software guide", "slug": "a-reliable-software-guide"}
        bad_article = {"article_markdown": "## Short draft"}
        good_article = {"article_markdown": "## Complete draft"}
        with patch.object(autopublisher, "normalize_article_payload", side_effect=[bad_article, good_article]), \
            patch.object(autopublisher, "enrich_article_metadata"), \
            patch.object(autopublisher, "deterministic_qa", side_effect=[["Article is too short"], []]), \
            patch.object(autopublisher, "ai_qa", return_value={"approved": True, "score": 0.9}):
            article, qa, feedback = autopublisher.generate_approved_article(
                ArticleClient(),
                topic,
                [],
                [],
                {"publishing": {"max_regeneration_attempts": 1}, "gemini": {"article_temperature": 0.4}},
                autopublisher.EventLog(),
            )

        self.assertEqual(article, good_article)
        self.assertEqual(qa["score"], 0.9)
        self.assertEqual(feedback, "")
        self.assertEqual(len(prompts), 2)
        self.assertIn("Short draft", prompts[1][0])
        self.assertEqual(prompts[0][1]["temperature"], 0.4)

    def test_generate_approved_article_abandons_repeated_unsupported_claims(self):
        prompts = []

        class ArticleClient:
            def generate_json(self, prompt, **_kwargs):
                prompts.append(prompt)
                return {"article_markdown": "## Draft\n\nUnsupported material claim."}

        issue = "Claim is not directly supported by its referenced source text: unsupported material claim"
        with patch.object(autopublisher, "normalize_article_payload", side_effect=lambda article, *_args, **_kwargs: article), \
            patch.object(autopublisher, "enrich_article_metadata"), \
            patch.object(autopublisher, "deterministic_qa", return_value=[issue]):
            article, qa, feedback = autopublisher.generate_approved_article(
                ArticleClient(),
                {"title": "Scoped topic", "slug": "scoped-topic"},
                [],
                [],
                {
                    "publishing": {"max_regeneration_attempts": 3, "max_stalled_repair_attempts": 2},
                    "gemini": {"article_temperature": 0.3},
                },
                autopublisher.EventLog(),
            )
        self.assertIsNone(article)
        self.assertIsNone(qa)
        self.assertEqual(len(prompts), 2)
        self.assertIn("Discard the previous draft completely", feedback)

    def test_choose_topic_excludes_topic_that_failed_generation(self):
        class TopicClient:
            def generate_json(self, *_args, **_kwargs):
                return {
                    "topics": [
                        {"title": "Failed topic", "slug": "failed-topic", "categories": ["technology"]},
                        {"title": "Fresh topic", "slug": "fresh-topic", "categories": ["technology"]},
                    ]
                }

        selected = autopublisher.choose_topic(
            TopicClient(),
            [],
            None,
            [],
            {"taxonomy": {"allowed_categories": ["technology"]}},
            autopublisher.EventLog(),
            excluded_slugs={"failed-topic"},
        )
        self.assertEqual(selected["slug"], "fresh-topic")

    def test_atom_link_selection_prefers_article_over_comments_endpoint(self):
        entry = ET.fromstring(
            '<entry xmlns="http://www.w3.org/2005/Atom">'
            '<link rel="replies" type="application/atom+xml" href="https://example.com/feeds/1/comments/default" />'
            '<link rel="self" type="application/atom+xml" href="https://example.com/feeds/1" />'
            '<link rel="alternate" type="text/html" href="https://example.com/2026/07/article.html" />'
            '</entry>'
        )
        self.assertEqual(
            autopublisher.first_text(entry, "link"),
            "https://example.com/2026/07/article.html",
        )
        self.assertFalse(autopublisher.is_publishable_source_url("https://example.com/feeds/1/comments/default"))
        self.assertTrue(autopublisher.is_publishable_source_url("https://example.com/2026/07/article.html"))

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

    def test_generated_chart_passes_text_collision_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chart.svg"
            autopublisher.render_bar_chart_svg(
                {"title": "Comparison", "unit": "Score", "data": [{"label": "Option A", "value": 10}]},
                path,
            )
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
        self.assertLess(len(prompt), 18000)
        self.assertIn("exactly 4 concise candidate topics", prompt)
        self.assertNotIn("exactly 8 candidate topics", prompt)
        self.assertNotIn("Apple/iOS and Android platform changes", prompt)

    def test_topic_selection_model_failure_uses_deterministic_research_fallback(self):
        class InvalidTopicClient:
            def generate_json(self, *_args, **_kwargs):
                raise autopublisher.GeminiTransientError("truncated JSON")

        research = [
            autopublisher.ResearchItem(
                source="Official docs",
                title="Network troubleshooting commands",
                url="https://example.com/network-troubleshooting",
                summary="Official network troubleshooting guidance.",
                published="2026-07-15",
                categories=["technology"],
                score=1.0,
                validated=True,
            )
        ]
        selected = autopublisher.choose_topic(
            InvalidTopicClient(),
            research,
            None,
            [],
            {
                "site": {"timezone": "UTC"},
                "taxonomy": {"allowed_categories": ["technology"], "balance_categories": ["technology"]},
                "research": {"topic_selection_max_prompt_characters": 18000},
            },
            autopublisher.EventLog(),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["source_urls"], [research[0].url])

    def test_github_changelog_is_an_allowed_official_source(self):
        config = autopublisher.load_config()
        url = "https://github.blog/changelog/2026-07-14-code-scanning-shows-ai-security-detections-on-pull-requests"
        self.assertTrue(autopublisher.is_trusted_source_url(url, config))

    def test_removed_broken_feeds_are_not_in_research_inventory(self):
        config = autopublisher.load_config()
        urls = {source["url"] for source in config["research"]["trusted_sources"]}
        self.assertNotIn(
            "https://techcommunity.microsoft.com/plugins/custom/microsoft/o365/custom-blog-rss?board.id=AzureActiveDirectory",
            urls,
        )
        self.assertNotIn("https://www.cisa.gov/cybersecurity-advisories/all.xml", urls)
        self.assertNotIn("https://www.ietf.org/feeds/ietf.xml", urls)

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

    def test_depleted_billing_opens_persistent_grounding_circuit(self):
        state = {"provider_cooldowns": {}}
        error = autopublisher.GeminiQuotaError(
            "Your prepayment credits are depleted. Please manage your project and billing."
        )
        with patch.object(autopublisher, "save_state") as save_state:
            opened = autopublisher.open_provider_circuit(
                state,
                "gemini_grounded_research",
                error,
                {"cost_control": {"gemini_billing_cooldown_hours": 168}},
            )
        self.assertTrue(opened)
        self.assertTrue(autopublisher.provider_circuit_open(state, "gemini_grounded_research"))
        self.assertEqual(
            state["provider_cooldowns"]["gemini_grounded_research"]["reason"],
            "billing_credit_depleted",
        )
        save_state.assert_called_once_with(state)

    def test_temporary_rate_limit_does_not_open_billing_circuit(self):
        state = {"provider_cooldowns": {}}
        with patch.object(autopublisher, "save_state") as save_state:
            opened = autopublisher.open_provider_circuit(
                state,
                "gemini_grounded_research",
                autopublisher.GeminiQuotaError("HTTP 429 rate limit"),
                {},
            )
        self.assertFalse(opened)
        self.assertFalse(state["provider_cooldowns"])
        save_state.assert_not_called()

    def test_topic_research_skips_grounding_while_billing_circuit_is_open(self):
        class NoCallClient:
            def grounded_research(self, _prompt):
                raise AssertionError("grounding must not be called while the circuit is open")

        state = {
            "provider_cooldowns": {
                "gemini_grounded_research": {
                    "until": autopublisher.iso_z(autopublisher.utc_now() + autopublisher.dt.timedelta(hours=1))
                }
            }
        }
        config = {
            "gemini": {"enable_google_search_grounding": True},
            "publishing": {"required_source_count": 3},
            "research": {"topic_source_max_items": 6},
        }
        scoped = autopublisher.collect_topic_research(
            NoCallClient(),
            {"title": "DNS troubleshooting", "categories": ["networking"]},
            [],
            config,
            autopublisher.EventLog(),
            state=state,
        )
        self.assertEqual(scoped, [])

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

    def test_publish_continues_with_fresh_topic_when_sources_are_insufficient(self):
        config = {
            "gemini": {"enable_google_search_grounding": False},
            "publishing": {"required_source_count": 2, "max_topic_attempts": 2},
            "site": {"content_dir": "content/posts"},
        }
        state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}}
        research = [
            autopublisher.ResearchItem("Official", "Source one", "https://trusted.example/one", "One", "", ["technology"], 1.0),
            autopublisher.ResearchItem("Official", "Source two", "https://trusted.example/two", "Two", "", ["technology"], 0.9),
        ]
        first_topic = {"title": "Source-poor topic", "slug": "source-poor", "categories": ["technology"]}
        second_topic = {"title": "Supported topic", "slug": "supported-topic", "categories": ["technology"]}
        article = {
            "title": "Supported topic",
            "slug": "supported-topic",
            "description": "Supported description",
            "categories": ["technology"],
            "tags": ["technology"],
            "sources": [],
            "article_markdown": "## Supported\n\nComplete article.",
        }

        class Client:
            def require_key(self):
                return None

        with patch.object(autopublisher, "load_config", return_value=config), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=[]), \
            patch.object(autopublisher, "collect_research", return_value=research), \
            patch.object(autopublisher, "enrich_research_snippets"), \
            patch.object(autopublisher, "GeminiClient", return_value=Client()), \
            patch.object(autopublisher, "choose_topic", side_effect=[first_topic, second_topic]), \
            patch.object(autopublisher, "collect_topic_research", side_effect=[[], research]), \
            patch.object(autopublisher, "generate_approved_article", return_value=(article, {"approved": True}, "")) as generate, \
            patch.object(autopublisher, "write_article_bundle", return_value=autopublisher.ROOT / "content/posts/supported-topic/index.md"), \
            patch.object(autopublisher, "save_state"), \
            patch.object(autopublisher, "write_publish_result"):
            result = autopublisher.run_publish(SimpleNamespace(dry_run=True))

        self.assertEqual(result, 0)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.args[1], second_topic)
        self.assertEqual(state["last_runs"]["publish"]["result"], "dry_run")

    def test_publish_uses_evergreen_recovery_after_dynamic_topics_fail(self):
        config = {
            "gemini": {"enable_google_search_grounding": False},
            "publishing": {
                "required_source_count": 1,
                "max_topic_attempts": 1,
                "max_evergreen_topic_attempts": 1,
            },
            "site": {"content_dir": "content/posts"},
        }
        state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}}
        research = [autopublisher.ResearchItem("Official", "Source", "https://trusted.example/one", "One", "", ["technology"], 1.0)]
        dynamic_topic = {"title": "Dynamic topic", "slug": "dynamic-topic", "categories": ["technology"]}
        evergreen_topic = {"title": "Evergreen topic", "slug": "evergreen-topic", "categories": ["technology"]}
        article = {
            "title": "Evergreen topic",
            "slug": "evergreen-topic",
            "description": "Evergreen description",
            "categories": ["technology"],
            "tags": ["technology"],
            "sources": [],
            "article_markdown": "## Evergreen\n\nComplete article.",
        }

        class Client:
            def require_key(self):
                return None

        with patch.object(autopublisher, "load_config", return_value=config), \
            patch.object(autopublisher, "load_state", return_value=state), \
            patch.object(autopublisher, "load_posts", return_value=[]), \
            patch.object(autopublisher, "collect_research", return_value=research), \
            patch.object(autopublisher, "enrich_research_snippets"), \
            patch.object(autopublisher, "GeminiClient", return_value=Client()), \
            patch.object(autopublisher, "choose_topic", return_value=dynamic_topic), \
            patch.object(autopublisher, "choose_evergreen_topic", return_value=evergreen_topic), \
            patch.object(autopublisher, "collect_topic_research", return_value=research), \
            patch.object(
                autopublisher,
                "generate_approved_article",
                side_effect=[(None, None, "dynamic failed"), (article, {"approved": True}, "")],
            ) as generate, \
            patch.object(autopublisher, "write_article_bundle", return_value=autopublisher.ROOT / "content/posts/evergreen-topic/index.md"), \
            patch.object(autopublisher, "save_state"), \
            patch.object(autopublisher, "write_publish_result"):
            result = autopublisher.run_publish(SimpleNamespace(dry_run=True))

        self.assertEqual(result, 0)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(generate.call_args.args[1], evergreen_topic)
        self.assertEqual(state["last_runs"]["publish"]["result"], "dry_run")

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

    def test_topic_relevance_accepts_approved_cluster_and_rejects_disallowed_topic(self):
        config = {
            "topic_scope": {
                "approved_categories": ["azure", "cybersecurity"],
                "disallowed_terms": ["celebrity", "smartphone launch"],
                "category_keywords": {
                    "azure": ["azure", "virtual network"],
                    "cybersecurity": ["security", "vulnerability"],
                },
            }
        }
        approved = autopublisher.topic_relevance_score(
            {
                "title": "Configure an Azure virtual network securely",
                "primary_category": "azure",
                "categories": ["azure"],
                "source_urls": ["https://learn.microsoft.com/a", "https://learn.microsoft.com/b"],
            },
            config,
        )
        rejected = autopublisher.topic_relevance_score(
            {"title": "Celebrity smartphone launch", "primary_category": "azure", "categories": ["azure"]},
            config,
        )
        self.assertGreaterEqual(approved["score"], 0.78)
        self.assertTrue(approved["approved"])
        self.assertEqual(rejected["critical_failure"], "disallowed_topic")
        self.assertEqual(rejected["score"], 0.0)

    def test_slug_normalization_deduplicates_words_and_preserves_complete_words(self):
        slug = autopublisher.slugify("Azure Azure Conditional Access Configuration Requirements", max_length=34)
        self.assertEqual(slug, "azure-conditional-access")
        self.assertNotIn("azure-azure", slug)

    def test_source_validation_requires_accessible_relevant_primary_page(self):
        item = autopublisher.ResearchItem(
            source="Microsoft Learn",
            title="Conditional Access report-only mode",
            url="https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-conditional-access-report-only",
            summary="Microsoft Entra Conditional Access supports report-only policies.",
            published="2026-01-01T00:00:00Z",
            categories=["entra-id"],
            score=1.0,
        )
        config = {
            "source_validation": {
                "trusted_domains": ["learn.microsoft.com"],
                "min_title_similarity": 0.05,
                "min_relevance_similarity": 0.05,
                "max_age_days": 1000,
                "max_release_age_days": 1000,
                "blocked_path_terms": ["/search"],
                "blocked_page_terms": ["access denied"],
            }
        }
        page = b"<html><head><title>Conditional Access report-only mode</title></head><body>Microsoft Entra Conditional Access report-only policies let administrators evaluate policy effects.</body></html>"
        headers = {"content-type": "text/html", "x-final-url": item.url}
        with patch.object(autopublisher, "http_request", return_value=(200, page, headers)):
            self.assertTrue(autopublisher.validate_research_item(item, config, autopublisher.EventLog()))
        self.assertTrue(item.validated)
        self.assertEqual(item.validation["reason"], "validated")

    def test_primary_page_text_prefers_main_documentation_content(self):
        document = """
        <html><body><nav>Navigation products pricing unrelated filler</nav>
        <main><h1>Using RBAC Authorization</h1><p>Roles and ClusterRoles define permissions. RoleBindings and ClusterRoleBindings grant those permissions to subjects.</p>
        <p>This documentation body contains enough directly relevant technical explanation for source-backed claims.</p></main>
        <footer>Footer links</footer></body></html>
        """
        text = autopublisher.extract_primary_page_text(document)
        self.assertIn("Roles and ClusterRoles", text)
        self.assertNotIn("Navigation products", text)

    def test_primary_page_text_prefers_unquoted_documentation_content_class(self):
        document = """
        <html><body><main><nav>Large documentation navigation unrelated to the claim.</nav>
        <div class=td-content><h1>Authorization</h1><p>Authentication happens before authorization.</p>
        <p>Admission control happens after authorization has completed, when the authorization decision allows the request.</p></div>
        <div id=pre-footer><p>Feedback and unrelated page chrome.</p></div></main></body></html>
        """
        text = autopublisher.extract_primary_page_text(document)
        self.assertIn("Admission control happens after authorization", text)
        self.assertNotIn("Large documentation navigation", text)
        self.assertNotIn("Feedback and unrelated", text)

    def test_source_validation_rejects_search_page_and_duplicate_url(self):
        config = {
            "source_validation": {
                "trusted_domains": ["learn.microsoft.com"],
                "blocked_path_terms": ["/search"],
                "max_candidates_per_run": 10,
            }
        }
        search = autopublisher.ResearchItem("Microsoft", "Azure search", "https://learn.microsoft.com/search/?terms=azure", "", "", ["azure"], 2.0)
        duplicate = autopublisher.ResearchItem("Microsoft", "Azure", "https://learn.microsoft.com/en-us/azure/guide", "", "", ["azure"], 1.0)
        duplicate_two = autopublisher.ResearchItem("Microsoft", "Azure copy", "https://learn.microsoft.com/en-us/azure/guide?utm_source=test", "", "", ["azure"], 0.5)
        with patch.object(autopublisher, "validate_research_item", side_effect=lambda item, *_: item is not search):
            accepted = autopublisher.validate_research_items([search, duplicate, duplicate_two], config, autopublisher.EventLog())
        self.assertEqual([item.title for item in accepted], ["Azure"])

    def test_claim_evidence_must_match_validated_source_text(self):
        source = autopublisher.ResearchItem(
            "Microsoft Learn",
            "Conditional Access report-only mode",
            "https://learn.microsoft.com/entra/report-only",
            "Evaluate a Conditional Access policy in report-only mode without enforcing it.",
            "2026-01-01T00:00:00Z",
            ["entra-id"],
            1.0,
            snippet="Microsoft Entra Conditional Access report-only mode evaluates policy results without enforcement.",
            validated=True,
        )
        config = {"publishing": {"required_claim_evidence_count": 1}, "source_validation": {"min_claim_confidence": 0.75, "min_claim_source_similarity": 0.05}}
        supported = {"claim_evidence": [{"claim": "Conditional Access has report-only mode", "supporting_sources": [source.url], "confidence": 0.97, "verified_at": "2026-07-15T00:00:00Z"}]}
        unsupported = {"claim_evidence": [{"claim": "Azure includes an unrelated hardware warranty", "supporting_sources": [source.url], "confidence": 0.97, "verified_at": "2026-07-15T00:00:00Z"}]}
        self.assertEqual(autopublisher.claim_evidence_issues(supported, config, [source]), [])
        self.assertTrue(any("not directly supported" in issue for issue in autopublisher.claim_evidence_issues(unsupported, config, [source])))

    def test_code_validation_rejects_syntax_secrets_and_unwarned_destructive_commands(self):
        markdown = """## Example\n\n```python\nif True print('broken')\n```\n\n```json\n{\"password\": \"realistic-secret-value\"}\n```\n\n```bash\nrm -rf /\n```\n"""
        issues = autopublisher.code_block_issues(markdown)
        self.assertTrue(any("Invalid python" in issue for issue in issues))
        self.assertTrue(any("embedded credential" in issue for issue in issues))
        self.assertTrue(any("destructive command" in issue for issue in issues))

    def test_valid_code_and_kubernetes_manifest_pass(self):
        markdown = """## Examples\n\n```python\nprint(\"safe\")\n```\n\n```json\n{\"enabled\": true}\n```\n\n```kubernetes\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: example\n```\n"""
        self.assertEqual(autopublisher.code_block_issues(markdown), [])

    def test_invalid_nested_yaml_fence_becomes_repair_feedback(self):
        markdown = """## Workflow permissions

```yaml
permissions:
  contents: read
```text
This nested fence is not valid YAML.
```
"""
        issues = autopublisher.code_block_issues(markdown)
        self.assertTrue(any("Invalid yaml code block" in issue for issue in issues))

    def test_shell_metavariables_are_normalized_before_syntax_validation(self):
        markdown = """## Check access

```bash
kubectl auth can-i <verb> <resource> --namespace <namespace>
```
"""
        normalized = autopublisher.normalize_shell_placeholders(markdown)
        self.assertIn("${VERB} ${RESOURCE}", normalized)
        self.assertIn("${NAMESPACE}", normalized)
        self.assertEqual(autopublisher.code_block_issues(normalized), [])

    def test_unlabeled_command_fence_is_labeled_and_validated(self):
        markdown = """## Check access

```
kubectl auth can-i get pods
```
"""
        normalized = autopublisher.normalize_code_fence_languages(markdown)
        self.assertIn("```bash", normalized)
        self.assertEqual(autopublisher.code_block_issues(normalized), [])

    def test_fence_language_normalizer_preserves_closing_fence(self):
        markdown = """## Check access

```bash
kubectl auth can-i get pods
```

Continue with prose.
"""
        normalized = autopublisher.normalize_code_fence_languages(markdown)
        self.assertEqual(normalized.count("```bash"), 1)
        self.assertIn("kubectl auth can-i get pods\n```\n\nContinue", normalized)

    def test_heading_hierarchy_and_generic_intro_are_rejected(self):
        markdown = "In today's rapidly evolving digital landscape, networking matters.\n\n## Start\n\n#### Skipped"
        self.assertTrue(autopublisher.introduction_issues(markdown))
        self.assertTrue(any("skips" in issue for issue in autopublisher.heading_hierarchy_issues(markdown)))

    def test_detailed_duplicate_detection_compares_intent_headings_and_sources(self):
        post = autopublisher.Post(
            Path("existing.md"), "configure-entra-mfa", "How to Configure Entra MFA", "Configure MFA for administrators.",
            "2026-01-01", ["entra-id"], ["entra-id"],
            "## Requirements\n\nUse Conditional Access.\n\n## Configuration\n\nCreate a policy.\n\n## Troubleshooting\n\nCheck sign-in logs.",
            {},
        )
        result = autopublisher.detailed_existing_similarity(
            title="Configure Microsoft Entra MFA", slug="configure-microsoft-entra-mfa",
            search_intent="how to configure entra mfa", body=post.body,
            categories=["entra-id"], tags=["entra-id"], source_urls=[], posts=[post],
        )
        self.assertGreater(result["heading"], 0.9)
        self.assertGreater(result["intent"], 0.7)

    def test_internal_links_reject_missing_and_noncanonical_targets(self):
        post = autopublisher.Post(Path("dns.md"), "dns-basics", "DNS basics", "DNS", "2026-01-01", ["dns"], ["networking"], "body", {})
        issues = autopublisher.internal_link_issues(
            "See [DNS](/posts/dns-basics/) and [missing](/posts/not-published/).",
            {"categories": ["networking"]}, {"publishing": {"minimum_internal_post_links": 2}}, [post],
        )
        self.assertFalse(any("topic-hub" in issue for issue in issues))
        self.assertTrue(any("broken or non-canonical" in issue for issue in issues))

    def test_internal_link_recovery_uses_supported_secondary_category_hub(self):
        posts = [
            autopublisher.Post(Path("rbac.md"), "rbac-basics", "RBAC basics", "RBAC", "2026-01-01", ["rbac"], ["cybersecurity"], "body", {}),
            autopublisher.Post(Path("kubernetes.md"), "kubernetes-basics", "Kubernetes basics", "Kubernetes", "2026-01-01", ["kubernetes"], ["developer-it-tools"], "body", {}),
        ]
        topic = {
            "title": "Troubleshooting Kubernetes RBAC",
            "search_intent": "Diagnose Kubernetes authorization",
            "categories": ["developer-it-tools", "cybersecurity"],
            "tags": ["kubernetes", "rbac"],
        }
        config = {"publishing": {"prefer_internal_links": 3, "minimum_internal_post_links": 2}}
        updated = autopublisher.ensure_contextual_internal_links("## Troubleshooting\n\nCheck access.", posts, topic, config)
        self.assertIn("/posts/rbac-basics/", updated)
        self.assertIn("/posts/rbac-basics/", updated)
        self.assertIn("/posts/kubernetes-basics/", updated)
        self.assertEqual(autopublisher.internal_link_issues(updated, topic, config, posts), [])

    def _write_rendered_fixture(self, root: Path, *, person: bool = False, noindex: bool = False, contact: bool = True) -> None:
        url = "https://www.compilemymind.com/"
        (root / "sitemap.xml").write_text(f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{url}</loc></url></urlset>', encoding="utf-8")
        schemas = [
            {"@context": "https://schema.org", "@type": "Organization", "name": "Compile My Mind"},
            {"@context": "https://schema.org", "@type": "WebSite", "name": "Compile My Mind"},
            {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []},
            {"@context": "https://schema.org", "@type": "CollectionPage", "name": "Home"},
            {"@context": "https://schema.org", "@type": "BlogPosting", "author": {"@type": "Organization"}, "publisher": {"@type": "Organization"}},
        ]
        if contact:
            schemas.append({"@context": "https://schema.org", "@type": "ContactPage", "name": "Contact"})
        if person:
            schemas.append({"@context": "https://schema.org", "@type": "Person", "name": "Fake Author"})
        scripts = "".join(f'<script type="application/ld+json">{json.dumps(item)}</script>' for item in schemas)
        robots = "noindex, follow" if noindex else "index, follow"
        document = f'''<!doctype html><html><head><link rel="canonical" href="{url}"><meta name="description" content="Test"><meta property="og:title" content="Test"><meta name="twitter:card" content="summary"><meta name="robots" content="{robots}">{scripts}</head><body><h1>Test</h1></body></html>'''
        (root / "index.html").write_text(document, encoding="utf-8")

    def test_rendered_audit_accepts_valid_organization_schema_and_sitemap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_rendered_fixture(root)
            issues = autopublisher.rendered_site_issues(root, {"site": {"base_url": "https://www.compilemymind.com/"}})
        self.assertEqual(issues, [])

    def test_rendered_audit_allows_optional_contact_page_to_be_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_rendered_fixture(root, contact=False)
            issues = autopublisher.rendered_site_issues(root, {"site": {"base_url": "https://www.compilemymind.com/"}})
        self.assertEqual(issues, [])

    def test_rendered_audit_rejects_person_schema_and_noindex_sitemap_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_rendered_fixture(root, person=True, noindex=True)
            issues = autopublisher.rendered_site_issues(root, {"site": {"base_url": "https://www.compilemymind.com/"}})
        self.assertTrue(any("Forbidden Person" in issue for issue in issues))
        self.assertTrue(any("Noindex page appears in sitemap" in issue for issue in issues))

    def test_tag_aliases_and_limits_enforce_controlled_vocabulary(self):
        config = {
            "taxonomy": {
                "controlled_tags": ["entra-id", "cybersecurity", "networking"],
                "tag_aliases": {"azure-ad": "entra-id", "cyber-security": "cybersecurity"},
                "max_tags_per_article": 2,
            }
        }
        tags = autopublisher.sanitize_tags(
            ["Azure AD", "azure-ad", "Cyber Security", "networking", "random-new-tag"],
            {"title": "Entra identity security", "categories": ["entra-id"]},
            config,
        )
        self.assertEqual(tags, ["entra-id", "cybersecurity"])

    def test_canonical_normalization_removes_tracking_and_normalizes_host(self):
        self.assertEqual(
            autopublisher.canonical_url("http://compilemymind.com/Posts/DNS/?utm_source=test#section"),
            "https://www.compilemymind.com/posts/dns/",
        )

    def test_formatting_only_revision_preserves_visible_modified_and_verification_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.md"
            frontmatter = {
                "title": "DNS",
                "date": "2026-01-01T00:00:00+00:00",
                "lastmod": "2026-02-01T00:00:00+00:00",
                "verification_date": "2026-02-01T00:00:00Z",
                "verification_version": 2,
                "categories": ["networking"],
                "tags": ["networking"],
            }
            path.write_text(autopublisher.compose_markdown(frontmatter, "## Existing\n\nBody"), encoding="utf-8")
            post = autopublisher.Post(path, "dns", "DNS", "Description", frontmatter["date"], ["networking"], ["networking"], "## Existing\n\nBody", frontmatter)
            autopublisher.update_post_file(
                post,
                {"updated_markdown": "## Existing\n\nMore readable body."},
                {"site": {"timezone": "UTC"}, "revalidation_intervals": {"default_days": 60}},
                substantive=False,
            )
            updated, _body = autopublisher.split_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["lastmod"], frontmatter["lastmod"])
        self.assertEqual(updated["verification_date"], frontmatter["verification_date"])
        self.assertEqual(int(updated["verification_version"]), 2)

    def test_failed_hugo_build_rolls_back_bundle_and_records_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "content" / "posts" / "safe-topic"
            index_path = bundle / "index.md"
            config = {
                "gemini": {"enable_google_search_grounding": False},
                "publishing": {"required_source_count": 1, "max_topic_attempts": 1},
                "site": {"content_dir": "content/posts"},
            }
            state = {"generated_posts": [], "maintenance_reviews": {}, "failures": [], "last_runs": {}, "rejected_articles": []}
            research = [autopublisher.ResearchItem("Official", "Safe source", "https://trusted.example/safe", "Safe", "", ["cybersecurity"], 1.0)]
            topic = {"title": "Safe topic", "slug": "safe-topic", "categories": ["cybersecurity"]}
            article = {"title": "Safe topic", "slug": "safe-topic", "categories": ["cybersecurity"], "tags": ["cybersecurity"]}

            class Client:
                def require_key(self):
                    return None

            def write_bundle(*_args, **_kwargs):
                bundle.mkdir(parents=True)
                index_path.write_text("generated", encoding="utf-8")
                return index_path

            with patch.object(autopublisher, "ROOT", root), \
                patch.object(autopublisher, "load_config", return_value=config), \
                patch.object(autopublisher, "load_state", return_value=state), \
                patch.object(autopublisher, "load_posts", return_value=[]), \
                patch.object(autopublisher, "collect_research", return_value=research), \
                patch.object(autopublisher, "enrich_research_snippets"), \
                patch.object(autopublisher, "GeminiClient", return_value=Client()), \
                patch.object(autopublisher, "choose_topic", return_value=topic), \
                patch.object(autopublisher, "generate_approved_article", return_value=(article, {"approved": True}, "")), \
                patch.object(autopublisher, "write_article_bundle", side_effect=write_bundle), \
                patch.object(autopublisher, "run_hugo_build", return_value=False), \
                patch.object(autopublisher, "save_state"), \
                patch.object(autopublisher, "write_publish_result"):
                result = autopublisher.run_publish(SimpleNamespace(dry_run=False))

        self.assertEqual(result, 0)
        self.assertFalse(bundle.exists())
        self.assertEqual(state["rejected_articles"][-1]["reason"], "build_failed")


if __name__ == "__main__":
    unittest.main()
