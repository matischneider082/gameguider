"""Color themes for the web UI and app. Each theme overrides the CSS custom properties of the default dark palette in
web.HTML_PAGE's :root, so a theme can recolor the page but never change its layout or behaviour. The choice is saved
in settings, not the browser: the page's port, and so its localStorage, changes every launch."""

from __future__ import annotations

from . import settings

DEFAULT = "dark"
LIGHT = {
    "bg": "#f6f4ef", "surface-1": "#ffffff", "surface-2": "#efece6", "surface-3": "#e5e1d8",
    "border": "#dcd7cd", "border-strong": "#bfb8aa", "text": "#1d1f24", "text-secondary": "#474c58", "muted": "#5a606e",
    "accent": "#6436cc", "accent-hover": "#5329b3", "accent-soft": "rgba(100, 54, 204, 0.10)", "accent-border": "rgba(100, 54, 204, 0.35)",
    "green": "#1d7656", "green-soft": "rgba(29, 118, 86, 0.10)", "green-border": "rgba(29, 118, 86, 0.35)",
    "blue": "#285fa6", "blue-soft": "rgba(40, 95, 166, 0.10)", "plum": "#8a4459", "plum-soft": "rgba(138, 68, 89, 0.10)",
    "danger": "#b03f1b", "danger-soft": "rgba(176, 63, 27, 0.10)",
    "on-accent": "#ffffff", "track": "rgba(0, 0, 0, 0.09)", "bar": "rgba(246, 244, 239, 0.94)", "bar-soft": "rgba(246, 244, 239, 0.7)",
}
THEMES = {  # name: (label, color-scheme, overrides of the :root palette)
    "dark": ("Dark", "dark", {}),
    "light": ("Light", "light", LIGHT),
    "contrast": ("High contrast", "dark", {
        "bg": "#000000", "surface-1": "#0b0b0b", "surface-2": "#161616", "surface-3": "#222222",
        "border": "#767676", "border-strong": "#b0b0b0", "text": "#ffffff", "text-secondary": "#ebebeb", "muted": "#d6d6d6",
        "accent": "#ffd60a", "accent-hover": "#ffe259", "accent-soft": "rgba(255, 214, 10, 0.16)", "accent-border": "rgba(255, 214, 10, 0.65)",
        "green": "#4ade9f", "green-soft": "rgba(74, 222, 159, 0.16)", "green-border": "rgba(74, 222, 159, 0.6)",
        "blue": "#78bbff", "blue-soft": "rgba(120, 187, 255, 0.16)", "plum": "#ffa3c4", "plum-soft": "rgba(255, 163, 196, 0.16)",
        "danger": "#ff7a59", "danger-soft": "rgba(255, 122, 89, 0.18)",
        "on-accent": "#000000", "track": "rgba(255, 255, 255, 0.22)", "bar": "rgba(0, 0, 0, 0.95)", "bar-soft": "rgba(0, 0, 0, 0.75)",
    }),
}
AUTO = "auto"  # Dark or Light, following the system's appearance
CHOICES = {AUTO: "Match system", **{name: label for name, (label, _scheme, _colors) in THEMES.items()}}


def current() -> str:
    """The saved theme; anything unknown (an older or edited config) is the default."""
    saved = settings.load().get("theme")
    return saved if saved in CHOICES else DEFAULT


def save(name: str) -> str:
    if name not in CHOICES:
        raise ValueError("Unknown theme.")
    settings.save("theme", name)
    return name


def css() -> str:
    """The CSS that applies each theme to <html data-theme="…">; the default dark palette needs none."""
    def block(selector, scheme, colors):
        return f"{selector} {{ color-scheme: {scheme}; " + " ".join(f"--{k}: {v};" for k, v in colors.items()) + " }"
    rules = [block(f':root[data-theme="{name}"]', scheme, colors) for name, (_label, scheme, colors) in THEMES.items() if colors]
    rules.append("@media (prefers-color-scheme: light) { " + block(f':root[data-theme="{AUTO}"]', "light", LIGHT) + " }")
    return "\n  ".join(rules)
