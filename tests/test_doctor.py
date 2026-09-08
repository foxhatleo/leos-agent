import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doctor


class ReadOnlyDoctor(unittest.TestCase):
    def test_clean_claude_check_does_not_create_config_or_claim_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "claude"
            local = root / "local"
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config), "LEOS_AGENT_LOCAL_PATH": str(local), "LEOS_AGENT_PRICE_REFRESH": "off"}):
                result = doctor.diagnose("claude")
            self.assertTrue(result["installation_current"])
            self.assertIn("Not established", result["runtime_activation"])
            self.assertFalse(config.exists())
            self.assertFalse(local.exists())

    def test_invalid_config_is_not_reported_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "routing.json").write_text("{broken")
            with patch.dict(os.environ, {"LEOS_AGENT_LOCAL_PATH": str(root), "CLAUDE_CONFIG_DIR": str(root / "claude"), "LEOS_AGENT_PRICE_REFRESH": "off"}):
                result = doctor.diagnose("claude")
            self.assertTrue(result["issues"])
            self.assertFalse(result["installation_current"])
