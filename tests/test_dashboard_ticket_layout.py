"""The ticket page never scrolls sideways (#865).

A grid track sized `auto` (or a bare `1fr`) has the min-content width of its
widest child as its floor, so one long unbroken line in a run transcript, a
path pane checkbox or a check name made a 260px column 2889px wide and the
whole page scroll sideways on a 1920px window. These checks read the CSS that
styles the classes the ticket page really renders.
"""
from __future__ import annotations

import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from dashboard import ticket_data  # noqa: E402
from dashboard.ticket import render  # noqa: E402
from test_dashboard_ticket import RECORD, FakeGitHub  # noqa: E402

LONG = "x" * 400


class Classes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = set()

    def handle_starttag(self, tag, attrs):
        self.found.update((dict(attrs).get("class") or "").split())


def ticket_classes():
    """Every class on a ticket page with long unbroken lines in each pane."""
    detail = ticket_data.TicketDetails(FakeGitHub()).get("example-owner/sample", 12)
    record = dict(RECORD, title="A long title " + LONG, body="- [x] " + LONG + "\n- [ ] short")
    events = [{"ts": "2026-10-14T10:00:00Z", "role": "user", "kind": "text", "text": "Brief\n" + LONG},
              {"ts": "2026-10-14T10:00:02Z", "role": "assistant", "kind": "tool_use", "text": "$ git log " + LONG},
              {"ts": "2026-10-14T10:00:03Z", "role": "tool", "kind": "tool_result", "text": "/a/" + LONG},
              {"ts": "2026-10-14T10:00:04Z", "role": "assistant", "kind": "text", "text": LONG}]
    runs = [{"id": 1, "attempt": 1, "outcome": "failed"}, {"id": 2, "attempt": 2}]
    parser = Classes()
    parser.feed(render(record, runs, 2, events, detail=detail))
    return parser.found


def css_rules(css):
    """(selector, declarations) for every rule, media-query rules included."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]+)\}", css):
        for selector in selectors.split(","):
            out.append((selector.strip(), body))
    return out


def declaration(body, name):
    match = re.search(r"(?<![\w-])%s\s*:\s*([^;]+)" % re.escape(name), body)
    return match.group(1).strip() if match else None


def last_classes(selector):
    """The classes of a selector's last compound: the element it styles."""
    last = re.split(r"[\s>+~]+", selector.strip())[-1]
    return set(re.findall(r"\.([\w-]+)", last))


def tracks_cannot_grow(value):
    """True when no track takes a child's min-content width as its floor:
    each is a fixed px width or minmax(0, ...)."""
    if not value:
        return False
    value = re.sub(r"repeat\(\s*\d+\s*,\s*(.+)\)$", r"\1", value.strip())
    tracks = re.findall(r"minmax\([^)]*\)|[^\s]+", value)
    return all(re.fullmatch(r"\d+(\.\d+)?px", track) or re.fullmatch(r"minmax\(\s*0(px)?\s*,[^)]*\)", track)
               for track in tracks)


def problems(css, classes):
    rules = css_rules(css)
    found = []
    for selector, body in rules:
        styled = last_classes(selector)
        if not styled or not styled <= classes:
            continue
        if declaration(body, "display") == "grid":
            columns = [declaration(b, "grid-template-columns") for s, b in rules if s == selector]
            if not any(tracks_cannot_grow(value) for value in columns if value):
                found.append("%s is a grid whose track grows with its widest child" % selector)
        white_space = declaration(body, "white-space")
        if white_space == "nowrap" and declaration(body, "overflow") != "hidden":
            found.append("%s never wraps and does not clip" % selector)
    for selector in (".ticket-header", ".ticket-columns"):
        if not any(s == selector and declaration(b, "overflow-wrap") == "anywhere" for s, b in rules):
            found.append("%s does not break long words (overflow-wrap: anywhere)" % selector)
    for selector, body in rules:
        if selector == ".pr-line span:first-child" and declaration(body, "white-space") == "nowrap":
            found.append("check names are cut to one line instead of wrapping")
    return found


class TicketPageWidth(unittest.TestCase):
    def test_a_long_unbroken_line_cannot_widen_the_ticket_page(self):
        classes = ticket_classes()
        self.assertIn("ticket-columns", classes)
        self.assertIn("tool-block", classes)
        self.assertIn("pr-line", classes)
        css = (ROOT / "dashboard/static/loom.css").read_text()
        self.assertEqual(problems(css, classes), [])

    def test_the_check_catches_a_grid_track_that_grows(self):
        classes = {"ticket-pane", "ticket-header", "ticket-columns"}
        css = (".ticket-pane { display: grid; gap: 16px; }\n"
               ".ticket-header { display: grid; grid-template-columns: minmax(0, 1fr); overflow-wrap: anywhere; }\n"
               ".ticket-columns { display: grid; grid-template-columns: 260px minmax(0, 1fr) 300px;"
               " overflow-wrap: anywhere; }\n")
        self.assertEqual(problems(css, classes),
                         [".ticket-pane is a grid whose track grows with its widest child"])


if __name__ == "__main__":
    unittest.main()
