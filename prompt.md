# Context

I have built a working Live Interpreter application (Windows, Python 3.14, PySide6, sounddevice/PortAudio, soxr, numpy, google-genai, keyring). It translates speech in both directions during calls, using Gemini Live and virtual audio cables for routing.

I now want to give a distributable copy to a small group of testers so they can run it on their own PCs and send feedback. The current code is a developer/debug build and is not ready to ship. It needs a clean-up and a separate release build first.

Packaging plan:
- **PyInstaller** bundles the app into one folder containing an .exe (one-folder build, not one-file). Bundle PySide6, sounddevice with its PortAudio library, soxr, numpy, google-genai, keyring, the `audio_test` clips and other assets.
- **Inno Setup** wraps that folder into an installer with an uninstaller and a first-run setup wizard.

Audio routing for this tester release uses **VB-Cable** plus **Voicemeeter Banana** (the current dev routing). Download pages:
- VB-Cable: https://vb-audio.com/Cable/index.htm
- Voicemeeter Banana: https://vb-audio.com/Voicemeeter/index.htm

Testers are not technical. Assume they cannot edit config files or use a terminal.

# Objective

Produce a **development plan** for distributing the application to testers. Ground it in what actually exists in the codebase today, not in assumptions. This is planning only.

# Task steps (in order)

1. **Explore the current codebase first.** Map the project structure, entry points, how settings, logging and credentials are handled, and what debug-only code exists. Read the existing bug-report feature before planning changes to it.
2. **Plan the bug report feature.** Reports must reach `renjie.xedge@gmail.com`.
3. **Plan the separate distribution build** (release clean-up, see the checklist below).
4. **Plan the PyInstaller one-folder package.**
5. **Plan the Inno Setup installer and uninstaller.**
6. **Plan the first-run setup wizard.**
7. **Plan the Voicemeeter/VB-Cable guide**, either inside the wizard or as a README, and recommend which.

# Analysis requirements

**Bug report feature**
- Report what exists today. Is anything sent, saved, or only displayed?
- Evaluate the options: a `mailto:` link, SMTP from the app, a small backend endpoint, or "save a report file and open the user's email client". Do not recommend embedding SMTP credentials or any secret in a distributed app. State the trade-offs of each (reliability for testers without a mail client, attachment size, privacy).
- Define what a report contains: app version, Windows version, selected device names, recent log lines, a user-written description. It must be **content-free**: no transcripts or audio.
- Say how the tester reviews and approves what is sent before it goes.

**Release build clean-up.** Check each item against the code and list what must change:
- Remove or disable `.env` loading, `debug_dumps/`, the `dump_raw_audio` tool, transcript logging of speech text, and any developer-only menus or flags.
- Remove the empty `ui/main_window.py` stub if it is unused.
- Confirm no API keys or secrets are in the source or the build.
- Decide how a release build is distinguished from a dev build (build flag, separate entry point, or version suffix). Prefer one codebase with a switch over a fork.
- Add a version string, shown in the app and in bug reports.
- Add a `--selftest` flag that loads all modules, lists audio devices and exits with a code, so the frozen build can be checked automatically.

**Credentials.** Testers need a way to run Gemini sessions. Company will give them a gemini-api key but there must be a way for testers to input it in to use it.

**PyInstaller**
- Propose the spec file contents: hidden imports, data files (`audio_test` clips, icons), binaries (PortAudio), and anything PySide6 needs.
- Flag likely problems: missing DLLs, `sounddevice` finding PortAudio, `keyring` backends, `google-genai` dynamic imports, antivirus false positives, and Python 3.14 support (check the PyInstaller version needed).
- Settings and logs must go to `%LOCALAPPDATA%`, not the install folder.

**Inno Setup**
- Cover: install location, shortcuts, a fixed application id so upgrades install over older versions, closing the running app first, uninstall behaviour (keep or delete settings, never remove VB-Audio drivers automatically), and the licence page.
- **Do not bundle VB-Audio's installers.** VB-Audio's terms forbid repackaging. The wizard and guide should link to the official download pages instead. Flag this as a decision for me if you disagree.
- Say whether the installer needs admin rights and how to avoid it if possible.
- Say what testers will see about an unsigned installer (Windows SmartScreen) and what the guide must tell them. Mention signing as a later step, not a blocker for testers.

**Setup wizard**
- Steps to consider: check whether VB-Cable and Voicemeeter are installed (and link to the guide if not), pick headset microphone and speakers, show the call-app audio settings the tester must choose, run the existing audio test, enter the Gemini key, and save settings.
- Reuse existing code (device detection, audio test, config saving) wherever it exists. List what is reused and what is new.
- Decide whether the wizard lives in Inno Setup pages, inside the app on first run, or both. Recommend one, with reasons. My expectation is that device checks belong in the app, because Inno cannot test audio.

**Voicemeeter and VB-Cable guide**
- Write the outline of a plain-language guide with screenshots needed: download, install, restart, which strips and buses to use, how to choose devices in the app and in the call app, how to test, and common mistakes.
- Verify the routing steps against the current code (device names, expected buses). Do not invent steps. If something cannot be confirmed from the code, mark it as an open question.

# Open questions to answer before finalising

Where the code or the docs cannot settle something, list it as an open question for me. Do not guess. At minimum, address:
- the confirmed bug-report email address
- how testers obtain a Gemini key
- whether testers' call app is WhatsApp Desktop only or others as well
- Windows versions to support (assume Windows 10 22H2 and Windows 11, x64 only)
- whether the installer should be signed for this tester release

# Deliverable

Produce a single markdown report containing:

1. **Current state summary**: what exists today, in plain terms, including the debug-only items found.
2. **Gap list for distribution**: columns: Area | Needed for release | Current state | Change required | Risk.
3. **Phased plan**, ordered by dependency, with an **exit check** for each phase. Suggested order: release clean-up, bug report, release flag and version, PyInstaller build, Inno installer, wizard, guide, tester trial. Mark which phases block the next.
4. **Pseudocode or outlines** only for what must be built: the bug-report sender, the `--selftest` flag, the PyInstaller spec, the Inno script skeleton and the wizard's device check. Use the codebase's real file and function names.
5. **Tester test matrix**: scenarios to run on real PCs (clean Windows 10 and 11, a PC with existing VB-Audio drivers, a PC without a mail client, upgrade over an older version, uninstall).
6. **Open questions** list, as above.

Markdown is to be saved in the ./guide folder.

# Constraints

- Do not modify any code or project files. This is planning and analysis only.
- Verify against the actual code. Do not assume file layout, function names or behaviour.
- Flag assumptions explicitly rather than filling gaps silently.
- Never place a secret (API key, SMTP password) in the plan's examples, the build or the installer.
- Keep the pseudocode scoped to distribution work. Do not redesign working audio or translation functionality.
- Where something cannot be determined from the code (no tests, no logs), say so explicitly.