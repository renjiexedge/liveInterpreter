"""VB-Audio checks (guide/distribution_plan.md §4.5).

With the current routing, B's translation is played into Voicemeeter's AUX strip.
If Voicemeeter Banana is closed, that playback most likely still succeeds, so the
candidate silently hears nothing: only a process check can tell."""
import logging
import subprocess

logger = logging.getLogger(__name__)

VOICEMEETER_BANANA_EXE = "voicemeeterpro.exe"  # checked in Task Manager > Details, 2026-10-12


def uses_voicemeeter(device_name: str) -> bool:
    return "voicemeeter" in device_name.casefold()


def voicemeeter_running() -> bool:
    """True if Voicemeeter Banana is running. Blocks for ~0.1 s (tasklist), so call
    it off the event loop. If the check itself fails, assume it's running: a
    false warning is worse than none."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {VOICEMEETER_BANANA_EXE}", "/NH"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Couldn't check whether Voicemeeter is running: %s", exc)
        return True
    return VOICEMEETER_BANANA_EXE.casefold() in out.casefold()
