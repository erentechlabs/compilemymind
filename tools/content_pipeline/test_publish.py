from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import publish  # noqa: E402


def valid_config() -> dict:
    return {
        "schema_version": 1,
        "model": "gemini-3.5-flash",
        "trusted_domains": ["example.com"],
        "article": {"minimum_words": 600, "minimum_sections": 2, "require_table": True},
        "briefs": [{
            "slug": "source-first-guide",
            "title": "Source First Guide",
            "description": "A description that explains the reader value clearly.",
            "reader_goal": "Learn a reliable process.",
            "categories": ["system-administration"],
            "tags": ["testing"],
            "sources": [
                {"title": "One", "url": "https://example.com/one"},
                {"title": "Two", "url": "https://example.com/two"},
                {"title": "Three", "url": "https://example.com/three"},
            ],
        }],
    }


class ContentPipelineTests(unittest.TestCase):
    def test_valid_source_first_config_has_no_issues(self):
        self.assertEqual(publish.config_issues(valid_config()), [])

    def test_config_rejects_source_bundle_with_fewer_than_three_sources(self):
        config = valid_config()
        config["briefs"][0]["sources"] = config["briefs"][0]["sources"][:2]
        self.assertIn("needs at least three sources", " ".join(publish.config_issues(config)))

    def test_config_rejects_untrusted_source(self):
        config = valid_config()
        config["briefs"][0]["sources"][0]["url"] = "https://untrusted.example/one"
        self.assertIn("not trusted", " ".join(publish.config_issues(config)))

    def test_extract_json_accepts_a_fenced_model_response(self):
        self.assertEqual(publish.extract_json("```json\n{\"body_markdown\": \"text\"}\n```"), {"body_markdown": "text"})

    def test_article_issues_enforce_length_sections_table_and_typed_fences(self):
        config = valid_config()
        short = "## One\n\nToo short.\n\n```\nGet-Service\n```"
        issues = publish.article_issues(short, config)
        self.assertTrue(any("words" in issue for issue in issues))
        self.assertTrue(any("H2" in issue for issue in issues))
        self.assertTrue(any("table" in issue for issue in issues))
        self.assertTrue(any("language" in issue for issue in issues))

    def test_pending_briefs_excludes_completed_and_existing_slugs(self):
        config = valid_config()
        state = {"completed_slugs": ["source-first-guide"]}
        self.assertEqual(publish.pending_briefs(config, state), [])

    def test_compose_post_adds_canonical_sources(self):
        brief = valid_config()["briefs"][0]
        sources = [
            publish.Source("One", "https://example.com/one", "evidence"),
            publish.Source("Two", "https://example.com/two", "evidence"),
            publish.Source("Three", "https://example.com/three", "evidence"),
        ]
        document = publish.compose_post(brief, {"body_markdown": "## Guide\n\nText"}, sources)
        self.assertIn("source_first: true", document)
        self.assertIn("## Sources", document)
        self.assertIn("https://example.com/three", document)


if __name__ == "__main__":
    unittest.main()
