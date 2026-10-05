"""Colour tokens for ui/style.qss (design.md §2). The stylesheet refers to them as
@name; ui/style.py substitutes the values before applying it. Severity colours
are not tokens: they stay as literals in the .qss."""

TOKENS = {
    # Brand (xedgeresource.com --ast-global-color-*)
    "brand-orange": "#FD8549",
    "brand-orange-hover": "#E9763C",  # ~8% darker, for primary-button hover
    "brand-orange-text": "#C4501A",   # orange text on white (#FD8549 is ~2.4:1)
    "brand-aqua": "#8AE0E5",
    "brand-sky": "#C8F0FF",
    "brand-sky-deep": "#96E4FA",
    # Neutrals
    "ink": "#2F3C4C",
    "ink-soft": "#566476",
    "muted": "#9CA7AB",
    "line": "#E9EAEC",
    "surface-tint": "#F6FDFE",
    "surface": "#FFFFFF",
    "navy-deep": "#3D4F63",
}
