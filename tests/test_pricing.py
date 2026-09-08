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

    def test_matching_conditional_schedules_compare_corresponding_rates(self):
        data = catalog(("openai/gpt-5.6-sol", 2, 10), ("openai/gpt-5.6-terra", 2, 12))
        for row in data["models"]:
            row["pricing"]["overrides"] = [{"min_prompt_tokens": 272000, "prompt": "4",
                                              "completion": str(int(row["pricing"]["completion"]) * 2)}]
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "over-ceiling")
        data["models"][1]["pricing"]["overrides"][0]["min_prompt_tokens"] = 100000
        self.assertEqual(pricing.compare("gpt-5.6-terra", "gpt-5.6-sol", data)["status"], "unknown")
