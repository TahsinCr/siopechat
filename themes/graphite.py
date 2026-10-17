"""Example programmable theme; trusted local Python executes at startup."""

ACCENT = "#b7a3e3"
THEME = {
    "id": "graphite",
    "tokens": {
        "background": "#1d1f21",
        "surface": "#25272a",
        "foreground": "#d8d8d8",
        "accent": ACCENT,
        "border": "#45484d",
    },
    "styles": {"tab.active": "bold #b7a3e3", "settings.title": "bold #b7a3e3"},
    "markdown": {"markdown.link": "underline #87b4d6"},
    "options": {
        "sidebar_width": 28,
        "message_width": 100,
        "composer_max_lines": 6,
        "tab_width": 26,
    },
}
