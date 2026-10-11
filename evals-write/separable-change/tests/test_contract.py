"""Acceptance checks for the rename and the backoff helper. Do not edit."""
import importlib
import unittest
from pathlib import Path

from shop import backoff, settings

SHOP = Path(__file__).resolve().parent.parent / "shop"
CONSUMERS = ("http_client", "payments", "inventory", "emailer", "search", "shipping")


class Rename(unittest.TestCase):
    def test_the_setting_has_its_new_name_and_value(self):
        self.assertEqual(settings.RETRY_LIMIT, 3)
        self.assertFalse(hasattr(settings, "MAX_RETRIES"))

    def test_no_module_uses_the_old_name(self):
        for path in sorted(SHOP.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertNotIn("MAX_RETRIES", path.read_text(encoding="utf-8"))

    def test_every_consumer_reads_the_renamed_setting(self):
        for name in CONSUMERS:
            with self.subTest(module=name):
                module = importlib.import_module("shop." + name)
                self.assertEqual(module.attempts(), settings.RETRY_LIMIT)


class RetryDelays(unittest.TestCase):
    def test_doubles_from_the_base(self):
        self.assertEqual(backoff.retry_delays(3), [100, 200, 400])

    def test_caps_each_delay(self):
        self.assertEqual(backoff.retry_delays(8), [100, 200, 400, 800, 1600, 3200, 5000, 5000])

    def test_custom_base_and_cap(self):
        self.assertEqual(backoff.retry_delays(4, base_ms=250, cap_ms=600), [250, 500, 600, 600])

    def test_no_attempts_no_delays(self):
        self.assertEqual(backoff.retry_delays(0), [])
