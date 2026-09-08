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
        # Preserve intentional dotfile symlinks on a write: resolving means the
        # bytes land on the target and the link keeps pointing there. A DELETE
        # is the opposite -- resolving would unlink the target and leave a
        # dangling link where the caller asked for the link itself to go.
        path = Path(path)
        if data is not None or not path.is_symlink():
            path = path.resolve()
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


def rollback(backup, boundary=None):
    """Undo an installation. `boundary` bounds the empty-directory cleanup."""
    backup = Path(backup)
    data = json.loads(backup.read_text())
    if data.get("schema") != 1 or not isinstance(data.get("files"), list):
        raise ValueError("invalid installation backup")
    tx = Transaction(backup.with_name(backup.stem + "-redo.json"))
    for entry in data["files"]:
        path = Path(entry["path"])
        current = path.read_bytes() if path.exists() else None
        original = base64.b64decode(entry["before"], validate=True) if entry["before"] is not None else None
        if not path.is_absolute():
            raise ValueError("backup paths must be absolute")
        if current == original:
            continue  # interrupted install had not applied this entry
        if digest(current) != entry["after_sha256"]:
            raise ValueError(f"{path} changed since installation; refusing rollback")
        tx.stage(path, original, entry["mode"])
    restored = len(tx.changes)
    tx.commit()
    prune_empty_dirs(tx.changes, boundary)
    # A completed rollback leaves nothing of its own behind: the redo receipt it
    # just wrote, and the backup it consumed, are both spent.
    tx.backup.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)
    return restored


def prune_empty_dirs(changes, boundary):
    """Remove directories emptied by a deletion, never past `boundary`."""
    if boundary is None:
        return
    boundary = Path(boundary).resolve()
    for path, entry in changes.items():
        if entry[1] is not None:
            continue
        parent = Path(path).parent
        while parent != boundary and boundary in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
