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

    def test_rollback_recovers_a_partially_applied_install(self):
        import base64
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            a.write_bytes(b"new")
            b.write_bytes(b"old")
            backup = root / "backup.json"
            backup.write_text(json.dumps({"schema": 1, "files": [
                {"path": str(path), "before": base64.b64encode(b"old").decode(),
                 "after_sha256": transaction.digest(b"new"), "mode": 0o600} for path in (a, b)]}))
            self.assertEqual(transaction.rollback(backup), 1)
            self.assertEqual(a.read_bytes(), b"old")
            self.assertEqual(b.read_bytes(), b"old")


class SymlinkAndCleanup(unittest.TestCase):
    def test_a_write_follows_a_symlink_but_a_delete_removes_the_link(self):
        """Resolving is right for a write -- a dotfiles symlink keeps pointing
        at its repo. Resolving a delete would unlink the target and leave the
        link dangling, which is the opposite of what the caller asked for."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real.md"
            target.write_bytes(b"original\n")
            link = root / "link.md"
            link.symlink_to(target)

            tx = transaction.Transaction(root / "backup.json")
            tx.stage(link, b"written\n")
            tx.commit()
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b"written\n")

            tx = transaction.Transaction(root / "backup2.json")
            tx.stage(link, None)
            tx.commit()
            self.assertFalse(link.is_symlink())
            self.assertTrue(target.is_file())

    def test_deleted_links_round_trip_including_dangling_relative_links(self):
        for target_name in ("target", "missing"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "target").write_bytes(b"untouched")
                link = root / "link"
                link.symlink_to(target_name)
                tx = transaction.Transaction(root / "backup.json")
                tx.stage(link, None)
                tx.commit()
                self.assertFalse(link.is_symlink())
                self.assertEqual(transaction.rollback(tx.backup), 1)
                self.assertTrue(link.is_symlink())
                self.assertEqual(transaction.os.readlink(link), target_name)
                self.assertEqual((root / "target").read_bytes(), b"untouched")

    def test_failed_transaction_restores_deleted_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            link, later = root / "link", root / "later"
            link.symlink_to("missing")
            tx = transaction.Transaction(root / "backup.json")
            tx.stage(link, None)
            tx.stage(later, b"new")
            real = transaction.replace
            def fail(path, data, mode=0o600):
                if path == later:
                    raise OSError("disk failure")
                return real(path, data, mode)
            with patch.object(transaction, "replace", side_effect=fail), self.assertRaises(OSError):
                tx.commit()
            self.assertEqual(transaction.os.readlink(link), "missing")
            self.assertFalse(later.exists())
            self.assertEqual(transaction.rollback(tx.backup), 0)

    def test_retargeted_link_is_a_concurrent_edit_even_with_identical_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("a", "b"):
                (root / name).write_bytes(b"same")
            link = root / "link"
            link.symlink_to("a")
            tx = transaction.Transaction(root / "backup.json")
            tx.stage(link, None)
            link.unlink()
            link.symlink_to("b")
            with self.assertRaisesRegex(OSError, "concurrent edit"):
                tx.commit()
            self.assertEqual(transaction.os.readlink(link), "b")

    def test_pruning_resolves_parent_aliases_but_does_not_escape_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            boundary = root / "config"
            (boundary / "deep").mkdir(parents=True)
            alias = root / "alias"
            alias.symlink_to(boundary, target_is_directory=True)
            transaction.prune_empty_dirs({alias / "deep" / "x": (b"x", None, 0o600)}, boundary)
            self.assertFalse((boundary / "deep").exists())
            outside = root / "outside"
            outside.mkdir()
            (boundary / "external").symlink_to(outside, target_is_directory=True)
            transaction.prune_empty_dirs({boundary / "external" / "x": (b"x", None, 0o600)}, boundary)
            self.assertTrue(outside.is_dir())
            self.assertTrue(boundary.is_dir())

    def test_rollback_leaves_neither_receipt_nor_emptied_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "agents" / "a.md"
            backup = root / "backup.json"
            tx = transaction.Transaction(backup)
            tx.stage(nested, b"installed\n")
            tx.commit()
            self.assertTrue(nested.is_file())

            self.assertEqual(transaction.rollback(backup, root), 1)
            self.assertFalse(nested.exists())
            self.assertFalse((root / "agents").exists())
            self.assertFalse(backup.exists())
            self.assertFalse((root / "backup-redo.json").exists())

    def test_pruning_never_climbs_past_its_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boundary = root / "config"
            leaf = boundary / "deep" / "x.md"
            leaf.parent.mkdir(parents=True)
            transaction.prune_empty_dirs({leaf: (b"x", None, 0o600)}, boundary)
            self.assertFalse((boundary / "deep").exists())
            self.assertTrue(boundary.is_dir())
            self.assertTrue(root.is_dir())


if __name__ == "__main__":
    unittest.main()
