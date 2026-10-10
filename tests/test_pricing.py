"""Price tests are offline. No regression test may issue a model request."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pricing


def catalog(*rows):
    return pricing.snapshot({"data": [
        {"id": name, "pricing": {"prompt": str(i), "completion": str(o)}}
        for name, i, o in rows]})


class ModelPrices(unittest.TestCase):
    def test_provider_aliases_and_dated_ids(self):
        data = catalog(("anthropic/claude-haiku-4.5", 1, 5))
        for name in ("claude-haiku-4-5-20251001", "haiku 4.5", "anthropic/claude-haiku-4.5",
                     "openrouter/anthropic/claude-haiku-4.5"):
            match = pricing.resolve(name, data)
            self.assertIsNotNone(match.model, name)
            self.assertEqual(match.requested, name)

    def test_bedrock_and_vertex_ids_share_the_first_party_identity(self):
        """The provider IDs Claude Code 2.1.296 lists for each model, plus the
        other cross-region prefixes it recognizes and the 1M-context suffix."""
        first_party = {
            "claude-opus-4-1-20250805": ("us.anthropic.claude-opus-4-1-20250805-v1:0", "anthropic.claude-opus-4-1-20250805-v1:0",
                                         "eu.anthropic.claude-opus-4-1-20250805-v1:0", "claude-opus-4-1@20250805"),
            "claude-sonnet-4-5-20250929": ("apac.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5@20250929",
                                           "global.anthropic.claude-sonnet-4-5-20250929-v1:0[1m]",
                                           "jp.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5@20250929[1m]"),
            "claude-3-5-sonnet-20241022": ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", "claude-3-5-sonnet-v2@20241022"),
            "claude-opus-4-20250514": ("au.anthropic.claude-opus-4-20250514-v1:0", "claude-opus-4@20250514"),
            "claude-haiku-4-5-20251001": ("us-gov.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5",
                                          "claude-haiku-4-5@20251001", "US.Anthropic.Claude-Haiku-4-5-20251001-V1:0"),
            "claude-opus-4-6": ("us.anthropic.claude-opus-4-6-v1",),
            "claude-fable-5-1": ("us.anthropic.claude-fable-5-1", "anthropic.claude-fable-5-1"),
        }
        data = json.loads(pricing.BUNDLED.read_text())
        for official, provider_ids in first_party.items():
            reference = pricing.resolve(official, data).report()["reference_model"]
            self.assertIsNotNone(reference, official)
            for name in provider_ids:
                with self.subTest(name=name):
                    self.assertEqual(pricing.identity(name), pricing.identity(official))
                    match = pricing.resolve(name, data)
                    self.assertEqual((match.requested, match.report()["reference_model"]), (name, reference))

    def test_only_the_known_provider_grammar_is_unwrapped(self):
        for name in ("xx.anthropic.claude-opus-4-1-20250805-v1:0", "us.amazon.nova-pro-v1:0", "anthropic.claude-v2:1",
                     "us.meta.llama4-maverick-17b-instruct-v1:0", "openai.gpt-oss-120b-1:0"):
            with self.subTest(name=name):
                self.assertIsNone(pricing.identity(name))
        data = json.loads(pricing.BUNDLED.read_text())
        self.assertEqual(pricing.resolve("claude-opus-4-1@2025", data).status, "unknown")

    def test_nearby_versions_preserve_tiers_and_sizes(self):
        data = catalog(("anthropic/claude-fable-5.1", 10, 50),
                       ("openai/gpt-5.6-sol", 2, 10), ("qwen/qwen3.8-27b", 1, 3),
                       ("deepseek/deepseek-v4-pro", 1, 2))
        for name in ("fable 5.2", "gpt-5.7-sol", "qwen3.9-27b", "deepseek-v5-pro"):
            self.assertEqual(pricing.resolve(name, data).status, "estimated", name)
        for name in ("gpt-5.7-luna", "qwen3.9-72b", "deepseek-v5-flash", "fable 99.2",
                     "random-provider/claude-fable-5.1", "gpt-5.7-sol:free"):
            self.assertEqual(pricing.resolve(name, data).status, "unknown", name)

    def test_ambiguous_dated_endpoints_are_unknown(self):
        data = catalog(("qwen/qwen3.8-max-0902", 1, 3), ("qwen/qwen3.8-max-0903", 2, 4))
        self.assertEqual(pricing.resolve("qwen3.8-max", data).status, "unknown")

    def test_zero_missing_crossovers_and_ceilings(self):
        data = catalog(("openai/gpt-5.6-sol", 2, 10), ("openai/gpt-5.6-terra", 2, 12),
                       ("qwen/qwen3.8-27b:free", 0, 0), ("moonshotai/kimi-k3", 1, 20))
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "over-ceiling")
        self.assertEqual(pricing.compare("qwen3.8-27b:free", "gpt-5.6-sol", data)["status"], "allowed")
        self.assertEqual(pricing.compare("kimi-k3", "gpt-5.6-sol", data)["status"], "unknown")
        self.assertEqual(pricing.compare("unlisted", "gpt-5.6-sol", data)["status"], "unknown")
        del data["models"][1]["pricing"]["completion"]
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "unknown")

    def test_nonfinite_and_negative_prices_are_rejected(self):
        for value in ("NaN", "Infinity", "-1", "not-money"):
            with self.assertRaises(pricing.PricingError):
                catalog(("openai/gpt-5.6-sol", value, 1))

    def test_conflicting_duplicate_model_prices_are_rejected(self):
        with self.assertRaisesRegex(pricing.PricingError, "duplicate model"):
            catalog(("openai/gpt-5.6-sol", 1, 2), ("openai/gpt-5.6-sol", 3, 4))

    def test_refresh_failure_preserves_catalog_and_throttles_retries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LEOS_AGENT_LOCAL_PATH=tmp):
            path = pricing.cache_path()
            path.write_text(json.dumps(catalog(("openai/gpt-5.6-sol", 2, 10))))
            before = path.read_bytes()
            def unavailable(*a, **kw):
                raise OSError("offline")
            with self.assertRaises(OSError):
                pricing.refresh(opener=unavailable)
            self.assertEqual(path.read_bytes(), before)
            status = json.loads(path.with_suffix(".status.json").read_text())
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["error"], "offline")
            self.assertFalse(pricing.refresh(opener=unavailable))

    def test_refresh_pagination_and_atomic_result(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LEOS_AGENT_LOCAL_PATH=tmp):
            calls = []
            def opener(url, timeout):
                calls.append(url)
                data = {"data": [{"id": "openai/gpt-5.6-sol", "pricing": {"prompt": "2", "completion": "10"}}]}
                if len(calls) == 1:
                    data["links"] = {"next": "/api/v1/models?offset=1"}
                    data["data"] = []
                return io.BytesIO(json.dumps(data).encode())
            self.assertTrue(pricing.refresh(opener=opener))
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(pricing.load()["models"]), 1)

    def test_bundled_catalog_covers_requested_models(self):
        data = json.loads(pricing.BUNDLED.read_text())
        for name in ("deepseek-v4-pro", "deepseek-v4-flash", "kimi-k3", "glm-5.2", "glm-5.3",
                     "glm-5.3-flash", "qwen3.8-max", "qwen3.8-27b", "fable 5.1", "opus 5",
                     "sonnet 5", "haiku 4.5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"):
            self.assertIsNotNone(pricing.resolve(name, data).model, name)

    def test_bundled_provenance_accounts_for_every_late_row(self):
        """fetched_at dates the full fetch, and doctor reports its age. A row
        added by hand later must be listed with the time it was added, so the
        snapshot never claims a fetch it did not make."""
        data = json.loads(pricing.BUNDLED.read_text())
        ids = {row["id"] for row in data["models"]}
        amended = set(data.get("amended_models", ()))
        self.assertLessEqual(amended, ids)
        if amended:
            self.assertGreaterEqual(data["amended_at"], data["fetched_at"])
        for row in data["models"]:
            if (row.get("created") or 0) > data["fetched_at"]:
                self.assertIn(row["id"], amended)
                self.assertLessEqual(row["created"], data["amended_at"])

    def test_matching_conditional_schedules_compare_corresponding_rates(self):
        data = catalog(("openai/gpt-5.6-sol", 2, 10), ("openai/gpt-5.6-terra", 2, 12))
        for row in data["models"]:
            row["pricing"]["overrides"] = [{"min_prompt_tokens": 272000, "prompt": "4",
                                              "completion": str(int(row["pricing"]["completion"]) * 2)}]
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "over-ceiling")
        # Different thresholds are compared for the same request size: Terra is
        # dearer below 100k, between 100k and 272k, and above 272k alike.
        data["models"][1]["pricing"]["overrides"][0]["min_prompt_tokens"] = 100000
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "over-ceiling")
        self.assertEqual(pricing.compare("gpt-5.6-sol", "gpt-5.6-terra", data)["status"], "allowed")

    def test_a_long_prompt_discount_does_not_hide_a_dearer_base_rate(self):
        """The bundled long-prompt Sonnet tier overlaps flat Opus. Ranges called
        that unknown, so an Opus child under a Sonnet parent passed the ceiling."""
        data = json.loads(pricing.BUNDLED.read_text())
        over = pricing.compare("opus", "claude-sonnet-4-5-20250929", data)
        self.assertEqual(over["status"], "over-ceiling")
        self.assertEqual(over["basis"], "base-rate")
        self.assertEqual(pricing.compare("opus", "claude-sonnet-4-20250514", data)["status"], "over-ceiling")
        # Cheaper at the base rate but dearer above 200k prompt tokens: a real
        # crossover, so it stays an explicit unknown rather than a block.
        self.assertEqual(pricing.compare("claude-sonnet-4-5", "opus", data)["status"], "unknown")

    def test_equal_base_rates_defer_to_the_long_prompt_tier(self):
        data = catalog(("openai/gpt-5.6-sol", 2, 10), ("openai/gpt-5.6-terra", 2, 10))
        data["models"][1]["pricing"]["overrides"] = [{"min_prompt_tokens": 200000, "prompt": "4", "completion": "20"}]
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "over-ceiling")
        data["models"][1]["pricing"]["overrides"] = [{"min_prompt_tokens": 200000, "prompt": "1", "completion": "5"}]
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "allowed")

    def test_conditions_other_than_prompt_size_are_compared_as_ranges(self):
        data = catalog(("openai/gpt-5.6-sol", 2, 10), ("openai/gpt-5.6-terra", 3, 12))
        data["models"][1]["pricing"]["overrides"] = [{"utc_days": ["saturday"], "prompt": "1", "completion": "5"}]
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "unknown")

    def test_nearby_versions_use_the_numerically_closest_neighbour(self):
        data = json.loads(pricing.BUNDLED.read_text())
        for name, expected in (("gpt-6.5-luna", "openai/gpt-6-luna"), ("claude-haiku-5", "anthropic/claude-haiku-5.5"),
                               ("claude-opus-6", "anthropic/claude-opus-5.5"), ("claude-sonnet-5-1", "anthropic/claude-sonnet-5")):
            match = pricing.resolve(name, data)
            self.assertEqual((match.status, match.report()["reference_model"]), ("estimated", expected), name)

    def test_one_gap_caps_estimates_across_and_within_major_versions(self):
        data = catalog(("openai/gpt-6-luna", 1, 2), ("openai/gpt-5.2-luna", 1, 2), ("deepseek/deepseek-v4-pro", 1, 2))
        self.assertEqual(pricing.resolve("gpt-6.9-luna", data).report()["reference_model"], "openai/gpt-6-luna")
        self.assertEqual(pricing.resolve("deepseek-v5-pro", data).status, "estimated")
        for name in ("gpt-7.1-luna", "gpt-5.2-luna-pro", "gpt-6-luna:free", "deepseek-v6-pro"):
            self.assertEqual(pricing.resolve(name, data).status, "unknown", name)
        # 5.6 is 0.4 from both 5.2 and 6; an equal gap prefers the same major version.
        self.assertEqual(pricing.resolve("gpt-5.6-luna", data).report()["reference_model"], "openai/gpt-5.2-luna")
