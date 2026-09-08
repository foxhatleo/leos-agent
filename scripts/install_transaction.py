"""Stage file changes, validate conflicts first, retain a reversible backup."""
import base64
import contextvars
import hashlib
import json
import os
from pathlib import Path
import tempfile

ACTIVE = contextvars.ContextVar("leo_install_transaction", default=None)


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def replace(path, data, mode=0o600):
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".leo-install-")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Transaction:
    def __init__(self, backup):
        self.backup = Path(backup)
        self.changes = {}

    def stage(self, path, data, mode=None):
        # Preserve intentional dotfile symlinks; store the resolved target so
        # rollback checks exactly the file that was changed.
        path = Path(path).resolve()
        original = path.read_bytes() if path.exists() else None
        mode = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o600)
        if original != data:
            self.changes[path] = (original, data, mode)

    def commit(self):
        if not self.changes:
            return
        for path, (before, _, _) in self.changes.items():
            current = path.read_bytes() if path.exists() else None
            if current != before:
                raise OSError(f"concurrent edit at {path}; installation aborted")
        backup = {"schema": 1, "files": [
            {"path": str(path), "before": base64.b64encode(before).decode() if before is not None else None,
             "after_sha256": digest(after), "mode": mode}
            for path, (before, after, mode) in self.changes.items()]}
        # Persist recovery before any target change. No prompts or credentials
        # are transmitted; configuration backups remain private on this machine.
        replace(self.backup, (json.dumps(backup, indent=2) + "\n").encode())
        applied = []
        try:
            for path, (before, after, mode) in self.changes.items():
                replace(path, after, mode)
                applied.append((path, before, mode))
        except BaseException:
            for path, before, mode in reversed(applied):
                replace(path, before, mode)
            raise


def rollback(backup):
    backup = Path(backup)
    data = json.loads(backup.read_text())
    if data.get("schema") != 1 or not isinstance(data.get("files"), list):
        raise ValueError("invalid installation backup")
    tx = Transaction(backup.with_name(backup.stem + "-redo.json"))
    for entry in data["files"]:
        path = Path(entry["path"])
        current = path.read_bytes() if path.exists() else None
        if digest(current) != entry["after_sha256"]:
            raise ValueError(f"{path} changed since installation; refusing rollback")
        original = base64.b64decode(entry["before"], validate=True) if entry["before"] is not None else None
        tx.stage(path, original, entry["mode"])
    tx.commit()
    return len(tx.changes)
