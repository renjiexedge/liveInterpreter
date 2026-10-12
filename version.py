"""Release switch (guide/distribution_plan.md §4.2). Phase 3 adds the version strings.

A frozen (PyInstaller) build is a release. `--dev` turns a frozen build back into
dev mode for debugging; `--release` runs from source in release mode, to check
release behaviour (no .env, locked WhatsApp combos, ...) without building.
Release turns off: raw audio dumps, .env loading, QSS live-reload and console
logging, and locks the WhatsApp device combos."""
import sys

RELEASE = (getattr(sys, "frozen", False) or "--release" in sys.argv) and "--dev" not in sys.argv
