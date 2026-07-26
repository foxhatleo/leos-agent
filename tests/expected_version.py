"""Single source of truth for the pinned release version.

This is the ONE place a release version is hand-updated. Every other test
must import EXPECTED_VERSION from here rather than re-pinning its own
literal copy of the version string.
"""

EXPECTED_VERSION = "6.0.0"
