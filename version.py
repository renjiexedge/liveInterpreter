"""Version and release switch (guide/distribution_plan.md §4.2).

__version__ is the one place to bump: .2, .3 … per tester build, so bug reports
identify it. The build number, FILE_VERSION (Windows version resource / Inno
VersionInfoVersion) and display_version() all derive from it.

A frozen (PyInstaller) build is a release. `--dev` turns a frozen build back into
dev mode for debugging; `--release` runs from source in release mode, to check
release behaviour (no .env, locked WhatsApp combos, ...) without building.
Release turns off: raw audio dumps, .env loading, QSS live-reload and console
logging, and locks the WhatsApp device combos."""
import sys

__version__ = "1.0.0-alpha.5"
DISPLAY_VERSION = "1.0.5 Alpha"
BUILD = int(__version__.rsplit(".", 1)[1])
FILE_VERSION = (1, 0, 5, BUILD)  # numeric only

RELEASE = (getattr(sys, "frozen", False) or "--release" in sys.argv) and "--dev" not in sys.argv


def display_version() -> str:
    """Shown in About, the window titles and bug reports: "1.0.3 Alpha (build 1)",
    with ", dev" added outside release so a dev run is never mistaken for a build."""
    return f"{DISPLAY_VERSION} (build {BUILD}{'' if RELEASE else ', dev'})"
