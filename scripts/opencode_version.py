"""The release of the `opencode` on PATH. Cheap (one `--version`, about 50 ms)
and fail-safe: anything it cannot establish is reported as unknown, never
guessed, so callers keep their OpenCode 1 behaviour."""
import re
import shutil
import subprocess


def probe(timeout=5):
    """(text, version): `text` is what `opencode --version` printed, or why
    there is nothing; `version` is (major, minor), or None when unknown."""
    binary = shutil.which("opencode")
    if not binary:
        return "opencode is not on PATH", None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        out = ""
    match = re.search(r"(\d+)\.(\d+)(?:\.\d+)*", out)
    return out or "unknown", (int(match.group(1)), int(match.group(2))) if match else None


def is_v2():
    """True only when the `opencode` on PATH reports a 2.x or later release."""
    version = probe()[1]
    return version is not None and version >= (2, 0)
