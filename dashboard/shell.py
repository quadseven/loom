"""The shell every dashboard screen renders inside.

page() wraps a screen's markup in one HTML document: from 960px a sticky left
nav with every view and the last tick's status; below that a sticky top bar,
a bottom tab bar (floor, goals, tickets, brain, more) and a "more" sheet with
the rest. Styles and behaviour come from /static/loom.css and /static/loom.js.

The screen's `body` is markup the screen built and escaped itself; every
string the shell adds is escaped here. Design: docs/design/floor/README.md.
"""
from __future__ import annotations

import html
from typing import Mapping, NamedTuple, Optional


class View(NamedTuple):
    key: str
    label: str
    href: str
    tab: str  # the mobile tab bar label, "" when the view is in the more sheet


VIEWS = (
    View("floor", "Floor", "/", "floor"),
    View("backlog", "Backlog", "/backlog", ""),
    View("goals", "Goals", "/goals", "goals"),
    View("tickets", "Tickets", "/tickets", "tickets"),
    View("brain", "Brain", "/brain", "brain"),
    View("hotspots", "Hot spots", "/hotspots", ""),
    View("improving", "Improving", "/improving", ""),
    View("lanes", "Lanes and models", "/lanes", ""),
    View("usage", "Usage", "/usage", ""),
    View("needs-you", "Needs you", "/needs-you", ""),
)
_KEYS = frozenset(v.key for v in VIEWS)

THEME_COOKIE = "loom_theme"
THEMES = ("light", "dark")

# The dot is never the only signal: each state prints its word beside it.
_STATUS_WORDS = {"ok": "tick ok", "failed": "tick failed", "missing": "no tick seen"}

FONTS = ("https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600"
         "&family=Geist+Mono:wght@400;500;600&display=swap")


class Status(NamedTuple):
    """The brain's last tick: "ok", "failed", or "missing" when none was read.

    `detail` follows the word, e.g. "14:30" or why the tick is missing."""
    state: str
    detail: str = ""


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def theme_from_cookie(header: Optional[str]) -> Optional[str]:
    """The theme pinned in a Cookie header, or None to follow the system."""
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == THEME_COOKIE and value in THEMES:
            return value
    return None


def _status(status: Status) -> str:
    detail = " " + esc(status.detail) if status.detail else ""
    return ('<span class="status status-%s"><span class="dot" aria-hidden="true"></span>'
            '<span><span class="status-word">%s</span>%s</span></span>'
            % (status.state, _STATUS_WORDS[status.state], detail))


def _link(view: View, active: Optional[str], cls: str, counts: Mapping[str, int],
          label: str = "") -> str:
    current = ' aria-current="page"' if view.key == active else ""
    n = counts.get(view.key)
    count = '<span class="nav-n num">%d</span>' % n if n else ""
    return '<a class="%s" href="%s"%s><span>%s</span>%s</a>' % (
        cls, esc(view.href), current, esc(label or view.label), count)


def _toggle(cls: str) -> str:
    # Hidden until loom.js runs: without the script it could not do anything.
    return ('<button class="%s" type="button" data-theme-toggle hidden>'
            '<span>Theme: <span data-theme-name>auto</span></span></button>' % cls)


def page(title: str, active: Optional[str], body: str, *, status: Status,
         theme: Optional[str] = None, counts: Optional[Mapping[str, int]] = None) -> str:
    """A full HTML document for one screen.

    `active` is the key of the view this screen belongs to (a ticket page is
    "tickets"), or None for a page outside the nav. `theme` pins "light" or
    "dark" (see theme_from_cookie); None follows prefers-color-scheme.
    `counts` puts a number beside a view, e.g. {"needs-you": 3}."""
    if active is not None and active not in _KEYS:
        raise ValueError("unknown view: %r" % (active,))
    if status.state not in _STATUS_WORDS:
        raise ValueError("unknown tick status: %r" % (status.state,))
    if theme is not None and theme not in THEMES:
        raise ValueError("unknown theme: %r" % (theme,))
    counts = counts or {}
    theme_attr = ' data-theme="%s"' % theme if theme else ""
    status_html = _status(status)

    nav = "".join("<li>%s</li>" % _link(v, active, "nav-link", counts) for v in VIEWS)
    tabs = "".join(_link(v, active, "tabbar-link", {}, v.tab) for v in VIEWS if v.tab)
    in_sheet = [v for v in VIEWS if not v.tab]
    more_on = " on" if any(v.key == active for v in in_sheet) else ""
    sheet = "".join(_link(v, active, "sheet-link", counts) for v in in_sheet)

    return """<!DOCTYPE html>
<html lang="en"%(theme)s>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s - loom</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="%(fonts)s">
<link rel="stylesheet" href="/static/loom.css">
<script src="/static/loom.js" defer></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="shell">
<nav class="nav" aria-label="Views">
<div class="nav-head"><a class="brand" href="/">loom.</a>%(status)s</div>
<ul class="nav-list">%(nav)s</ul>
<div class="nav-foot">%(toggle)s</div>
</nav>
<div class="content">
<header class="topbar"><a class="brand" href="/">loom.</a>%(status)s</header>
<main class="main" id="main" tabindex="-1">
%(body)s
</main>
</div>
</div>
<nav class="tabbar" aria-label="Views">%(tabs)s<a class="tabbar-link%(more_on)s" href="#more" data-sheet="more" aria-controls="more" aria-expanded="false">more</a></nav>
<div class="sheet" id="more">
<a class="sheet-backdrop" href="#" data-sheet-close aria-hidden="true" tabindex="-1"></a>
<div class="sheet-panel" role="dialog" aria-label="More views">%(sheet)s%(sheet_toggle)s</div>
</div>
</body>
</html>
""" % {
        "theme": theme_attr, "title": esc(title), "fonts": esc(FONTS),
        "status": status_html, "nav": nav, "toggle": _toggle("btn btn-quiet"),
        "body": body, "tabs": tabs, "more_on": more_on, "sheet": sheet,
        "sheet_toggle": _toggle("sheet-link"),
    }
