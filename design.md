# Front-end Design: Xedge Live Interpreter

This document covers the design of the desktop UI: how windows are structured and how they look.
Behaviour (sessions, audio, health) is covered in `CLAUDE.md` and `guide/`.

## 1. Application shell (TransGull-style)

The reference screenshots are in `guide/`:
- `Transgull Main Window.png`: the home screen layout.
- `Transgull feature Window (Simultaneous Interpretation).png`: only shows that clicking a feature
  opens a **completely separate window**. Its content is **not** a reference for the Live
  Interpreter window (see §6).

A **main window** is the home screen. Clicking a feature button opens that feature in its own
top-level window. The main window stays open behind it.

What we take from TransGull's main window:
- A single header row with the logo on the left and account/actions on the right.
- Pill-shaped feature buttons (icon + label) in a grid at the top of the content.
- A soft, near-white background with a faint tint.

What we leave out: the left sidebar (Personal space, search, record categories, folders), the
right sidebar (FAQ, Get help, promo carousel), the records area, and the Update / Shells
Top-up buttons.

```
┌───────────────────────────────────────────────────────────────────────┐
│ [logo] Xedge Resource              [⚑ Report a bug]  (◉) Name   _ □ × │  header
├───────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ╭──────────────────────╮ ╭─────────────────────────────────╮         │
│  │ 🎙  Live Interpreter  │ │ ☰  Live Transcript (Coming soon) │         │  feature buttons
│  ╰──────────────────────╯ ╰─────────────────────────────────╯         │
│                                                                       │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
          │ click "Live Interpreter"
          ▼
┌─ Live Interpreter ──────────────────── _ □ × ┐
│  (ui/live_interpreter.py, in its own        │   new top-level window
│   layout: see §6)                            │
└──────────────────────────────────────────────┘
```

### Main window header

| Element | Position | Behaviour |
|---|---|---|
| Logo | Left | Not interactive. The site's wide logo (`ui/assets/logo_wide.png`, which already contains the "Xedge resource" wordmark) at 36 px high. |
| Report a bug | Right | Secondary pill button with a bug icon. Opens a small dialog: a description box, plus "Include system details" (checked by default). Where reports go is still to be decided (email, form, or issue tracker); until then they're saved to the gitignored `bug_reports/`. "Include recent logs" needs a log file, which the app doesn't write yet. |
| User profile | Right, after the bug button | Round avatar (32 px; shows initials if there's no picture) and display name. Clicking it opens a menu. There are no user accounts yet, so for v1 this is a placeholder: the name comes from config and the menu only has "About". |
| Window controls | Far right | See "Title bar" below. |

**Title bar:** TransGull draws its own title bar, so the header and the window controls share one
row. For v1, keep the **native Windows title bar** and put the header row directly under it. A
frameless window loses Windows snapping, resizing and the native shadow unless all of that is
rebuilt. Merging them later is optional (Qt 6's `startSystemMove()`/`startSystemResize()` can
restore most of it).

### Main window content

Only the feature buttons: a left-aligned grid that wraps (TransGull fits three per row).
New features add an entry to `FEATURES` in `ui/main_window.py`.

1. **Live Interpreter**: opens `ui/live_interpreter.py`.
2. **Live Transcript (coming soon)**: a feature button like Live Interpreter, but disabled and
   tagged "Coming soon". It becomes clickable once it has a window (a `create` function in
   `FEATURES`).

### Files

| File | Role |
|---|---|
| `ui/main_window.py` | **Main window** (`QMainWindow`): header and feature button grid (`FEATURES`). |
| `ui/bug_report.py` | Report a bug dialog. |
| `config/ui_config.py` | `UserProfile` (header name/initials) and `BugReportConfig`. |
| `ui/live_interpreter.py` | **Live Interpreter feature window** (`LiveInterpreterWindow`). `python -m ui.live_interpreter` runs it on its own during development. |
| `main.py` | Entry point: creates the `QApplication`, calls `apply_stylesheet`, and shows the main window. |
| `ui/style.qss` / `ui/style.py` | One app-wide stylesheet, shared by the main window and every feature window. |

### Window behaviour rules

- **Only one window per feature.** Clicking a feature button whose window is already open raises and
  focuses that window (`show()`, `raise_()`, `activateWindow()`) instead of creating a second one.
  This is the window-level version of Start's idempotency guard: two interpreter windows would
  fight over the same audio devices.
- The main window holds a reference to each open feature window (`self._windows: dict[str, QWidget]`).
  When a window is destroyed, its entry is removed.
- Feature windows are top-level windows (no parent, as in the TransGull screenshot), not dialogs
  or panels inside the main window. They get their own taskbar entry (`QApplication.alert`
  flashes it) and are not modal. Staff will usually look at WhatsApp, not at these windows.
- Feature windows don't repeat the main window's header (logo, bug report, profile).
- **Closing a feature window that has a session running** asks for confirmation. If confirmed,
  it calls `SessionManager.stop()` and closes only after the session reports `idle`, so audio
  threads are never orphaned.
- **Closing the main window** while feature windows are open: closing it closes them too, using
  the same rule above.
- A missing API key is reported in the Live Interpreter window (`API_KEY_MISSING`), as it is now,
  not in the main window. The main window has no status footer.

## 2. Colour palette (from xedgeresource.com)

These values come from the site's Astra theme globals (`--ast-global-color-0…8`) and its
homepage Elementor CSS. The site's Elementor kit "primary/accent" (`#6EC1E4`, `#61CE70`) are
Elementor's defaults and aren't used on the page, so they're left out.

### Brand

| Token | Hex | Site use | App use |
|---|---|---|---|
| `brand.orange` | `#FD8549` | Links, selection, focus borders (`--ast-global-color-0`) | Primary button fill, focus ring, feature button hover border |
| `brand.orange-text` | `#C4501A` | (derived) | Orange **text** on white. `#FD8549` on white is only ~2.4:1 contrast |
| `brand.aqua` | `#8AE0E5` | Secondary accent (`--ast-global-color-1`) | Secondary accents, "Coming soon" pill |
| `brand.sky` | `#C8F0FF` | Icon bubbles on the homepage | Feature button icon colour, avatar background |
| `brand.sky-deep` | `#96E4FA` | Hero gradient end (white → this) | Faint main-window background tint (like TransGull's) |
| `brand.periwinkle` | `#9097F0` | Section gradient end | Sparingly: decorative gradient only |

### Neutrals

| Token | Hex | Site use | App use |
|---|---|---|---|
| `ink` | `#2F3C4C` | Headings, site title (`--ast-global-color-2`) | Headings, primary text, text on orange buttons |
| `ink.soft` | `#566476` | Body text (`--ast-global-color-3`) | Body text, labels |
| `muted` | `#9CA7AB` | (`--ast-global-color-7`) | Hints (`role="hint"`), disabled text, placeholders |
| `line` | `#E9EAEC` | Borders (`--ast-global-color-6`) | Card/input borders, separators |
| `surface.tint` | `#F6FDFE` | Page background (`--ast-global-color-4`) | Window background |
| `surface` | `#FFFFFF` | (`--ast-global-color-5`) | Cards, inputs, issue panel |
| `navy.deep` | `#3D4F63` | Custom kit colour | Hover state of `ink` elements |

### Semantic (status) colours

The existing severity colours in `ui/style.qss` stay as they are, and stay **only** in the `.qss`:

| `severity` | Hex |
|---|---|
| `ok` | `#2E7D32` |
| `warning` | `#F9A825` |
| `problem` | `#C62828` |

**Rule: brand orange is never used to show status.** It sits close to the warning amber. A
status is always a severity colour **plus text**, never colour alone. Orange only marks
"this is the action" or "this has focus".

### Contrast

| Pair | Ratio | OK for |
|---|---|---|
| `ink` on `surface` | ~11:1 | All text |
| `ink.soft` on `surface` | ~6:1 | Body text |
| `ink` on `brand.orange` | ~4.6:1 | Button labels (bold) |
| white on `brand.orange` | ~2.4:1 | **Don't use** |
| `muted` on `surface` | ~2.5:1 | Disabled/decorative only. Don't use it for hints staff must read; use `ink.soft` italic instead |

## 3. Typography

The site uses **Nunito Sans** for body text (400/700) and **Be Vietnam Pro** (600) for headings.
Both are Google Fonts under the SIL Open Font License, so they can be shipped with the app.

- Put the `.ttf` files in `ui/fonts/`, then load them in `ui/style.py` with
  `QFontDatabase.addApplicationFont` before the stylesheet is applied.
- Fall back to `"Segoe UI"` (Windows) if loading fails.

| Role | Font | Size | Weight |
|---|---|---|---|
| Header app name | Be Vietnam Pro | 18 px | 600 |
| Window/section heading | Be Vietnam Pro | 15 px | 600 |
| Feature button label | Be Vietnam Pro | 14 px | 600 |
| Body, labels, combos | Nunito Sans | 13 px | 400 |
| Buttons | Nunito Sans | 13 px | 700 |
| Hints, footer | Nunito Sans | 12 px | 400 italic (hints) |
| Status line | Nunito Sans | 13 px | 700 |

## 4. Shape and spacing

From the site: rounded corners of 12 / 16 / 24 px, soft shadows, plenty of white space.

- **Radius:** inputs and buttons 8 px, cards and the issue panel 12 px, main-window feature
  buttons and header pills fully rounded (radius = half their height, as in TransGull).
- **Spacing:** 4 px base unit. Use 8 / 12 / 16 / 24. Window margins are 24 px on the main
  window and 16 px in feature windows (they're denser).
- **Shadows:** QSS has no `box-shadow`. Use `QGraphicsDropShadowEffect` (blur 24, offset 0,4,
  `rgba(0,0,0,0.05)`, matching the site) on main-window feature buttons only. Don't put it on
  dense feature windows; it costs repaint time.
- **Borders:** 1 px `line` on cards and inputs. Focus changes the border to 2 px `brand.orange`.

## 5. Components

### Feature button (`QPushButton[role="feature"]`)

Matches TransGull's feature pills: a wide, fully rounded button with an icon and a one- or
two-line label, and no description text.

- `surface` background, 1 px `line` border, radius = half the height, 280×52 px,
  16 px left padding, 20 px icon then a 10 px gap, label in `ink`.
- Each feature has its own icon colour (TransGull colour-codes them). Live Interpreter uses
  `brand.orange` for its microphone icon.
- Hover: border `brand.orange`. Pressed: background `surface.tint`. Keyboard focus: 2 px
  `brand.orange` border.
- `[state="open"]`: when that feature's window already exists, a small dot after the label shows
  it's running. Clicking it brings the window to the front.
- `[state="soon"]`: a feature that isn't built yet. The button is disabled, with a
  `surface.tint` background, its label in `ink.soft`, and a "Coming soon" tag on the right
  (`brand.aqua` background, `ink` 11 px bold text, fully rounded).

### Header pills (Report a bug, profile)

- Report a bug: like a secondary button but fully rounded, with an icon and label, about 32 px high.
- Profile: a 32 px round avatar (`brand.sky` with `ink` initials if there's no picture) and the
  name in `ink`, with no border. The hover background is `surface.tint`. The pill grows to fit
  the name up to 180 px of name (`PROFILE_NAME_MAX_WIDTH`). A longer name is cut short with "…",
  and the full name is shown in the tooltip.

### Buttons

| Variant | Selector | Look |
|---|---|---|
| Primary (Start) | `QPushButton[variant="primary"]` | `brand.orange` fill, `ink` bold text; hover is 8% darker |
| Secondary (Test Audio Devices, Mute) | `QPushButton` (default) | `surface` fill, 1 px `line` border, `ink` text |
| Danger (Stop) | `QPushButton[variant="danger"]` | `surface` fill, `problem` border and text |
| Disabled (all) | `:disabled` | `surface.tint` fill, `muted` text, no border colour |

There's only one primary button per window.

### Inputs (combos)

`surface` background, 1 px `line` border, 8 px radius, 6×10 px padding, `ink` text. The dropdown
list uses a `brand.sky` highlight with `ink` text.

### Status and issue panel

Keep what's already there (`QLabel#statusDot[severity]`, `QFrame#issueRow[severity]`). Only the
surrounding container changes: `surface` background, 12 px radius, 1 px `line`.

## 6. Live Interpreter window layout

This window keeps its own layout. **Don't model it on TransGull's feature window**
(the "Select Simultaneous Interpretation Mode" screen with One-Way/Two-Way cards): that
screenshot only shows that features open in a separate window. There's no mode-selection step;
the Live Interpreter is always two-way and opens straight into its controls.

It shares the main window's palette, fonts and component styles (§2–§5) through the app-wide
stylesheet. The content is the same as `ui/live_interpreter.py`, grouped into cards so there's a
clear order:

1. **Header**: title "Live Interpreter", overall status dot and status line.
2. **Languages card**: "Staff hears" (disabled) and "Candidate hears".
3. **Audio devices card**: two columns, A (WhatsApp → headset) and B (headset → WhatsApp),
   each with its capture/playback combos, WhatsApp hint and direction dot/status.
4. **Actions row**: Test Audio Devices (secondary) on the left; Start (primary) and Stop
   (danger) on the right.
5. **Mute row**: per-direction mute buttons, shown while a session is running.
6. **Issue panel**: fills the remaining height.

## 7. Implementation notes

- Follow the existing styling rules in `CLAUDE.md`: everything goes in `ui/style.qss`, never
  per-widget `setStyleSheet`; use type selectors for defaults, `#objectName` for one-offs and
  `[property="value"]` for groups; change states with `set_style_property()`.
- QSS has no variables. Keep the palette in one place: add the tokens above as `@name`
  placeholders in `style.qss` (for example `@brand-orange`), and have `apply_stylesheet`
  replace them from a dict in `ui/theme.py` before calling `setStyleSheet`. Severity colours
  stay as literal values in the `.qss`, as they are now.
- Light theme only for v1. A dark theme would just be a second token dict.
- Verify styling headlessly as described in `CLAUDE.md` (`QT_QPA_PLATFORM=offscreen`).
- Assets: the site logo (`Logo_refreshed.png`) goes in `ui/assets/` for the main-window header
  and the window icon. Feature and header icons are SVGs in `ui/assets/icons/`, loaded with
  `QIcon`. Icons are recoloured per feature, so keep them single-colour line icons.
