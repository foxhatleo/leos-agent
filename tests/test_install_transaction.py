from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_transaction as transaction


class Transactions(unittest.TestCase):
    def test_validation_precedes_all_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            a.write_text("original")
            tx = transaction.Transaction(root / "backup.json")
            tx.stage(a, b"replacement")
            tx.stage(b, b"new")
            a.write_text("concurrent edit")
            with self.assertRaises(OSError):
                tx.commit()
            self.assertFalse(b.exists())
            self.assertEqual(a.read_text(), "concurrent edit")

    def test_failure_rolls_back_prior_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            a.write_text("original")
            tx = transaction.Transaction(root / "backup.json")
            tx.stage(a, b"replacement")
            tx.stage(b, b"new")
            real = transaction.replace
            def fail(path, data, mode=0o600):
                if path == b.resolve():
                    raise OSError("disk failure")
                return real(path, data, mode)
            with patch.object(transaction, "replace", side_effect=fail), self.assertRaises(OSError):
                tx.commit()
            self.assertEqual(a.read_text(), "original")
            self.assertFalse(b.exists())

    def test_rollback_preserves_permissions_and_refuses_later_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            a.write_text("original")
            a.chmod(0o640)
            tx = transaction.Transaction(root / "backup.json")
            tx.stage(a, b"replacement")
            tx.commit()
            self.assertEqual(a.stat().st_mode & 0o777, 0o640)
            a.write_text("user edited")
            with self.assertRaises(ValueError):
                transaction.rollback(root / "backup.json")
            a.write_text("replacement")
            transaction.rollback(root / "backup.json")
            self.assertEqual(a.read_text(), "original")
            self.assertEqual(a.stat().st_mode & 0o777, 0o640)
