"""The dashboard shell, its tokens and its one script (#287)."""
from __future__ import annotations

import math
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import shell  # noqa: E402

STATIC = ROOT / "dashboard" / "static"
CSS = (STATIC / "loom.css").read_text(encoding="utf-8")
JS = (STATIC / "loom.js").read_text(encoding="utf-8")

LABELS = ["Floor", "Backlog", "Goals", "Tickets", "Brain", "Hot spots", "Improving",
          "Lanes and models", "Usage", "Needs you"]


class Tree(HTMLParser):
    """Every element with its attributes, its text and the classes of its
    ancestors, enough to ask which links sit in which nav."""

    VOID = {"meta", "link", "br", "img", "input", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.elements = [], []

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "text": "",
              "within": [c for e in self.stack for c in e["attrs"].get("class", "").split()]}
        self.elements.append(el)
        if tag not in self.VOID:
            self.stack.append(el)

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()["tag"] == tag:
                break

    def handle_data(self, data):
        for el in self.stack:
            el["text"] += data


def parse(doc):
    tree = Tree()
    tree.feed(doc)
    return tree.elements


def links(elements, region):
    return [e for e in elements if e["tag"] == "a" and region in e["within"]]


def render(active="floor", **kw):
    kw.setdefault("status", shell.Status("ok", "14:30"))
    return shell.page("Floor", active, '<p class="probe">body</p>', **kw)


class PageTests(unittest.TestCase):
    def test_desktop_nav_links_every_view_and_marks_the_active_one(self):
        nav = links(parse(render("hotspots")), "nav-list")
        self.assertEqual([a["text"].strip() for a in nav], LABELS)
        for a in nav:
            self.assertTrue(a["attrs"].get("href", "").startswith("/"), a)
        current = [a["text"] for a in nav if a["attrs"].get("aria-current") == "page"]
        self.assertEqual(current, ["Hot spots"])

    def test_mobile_tab_bar_and_more_sheet_hold_every_view(self):
        elements = parse(render("tickets"))
        tabs = links(elements, "tabbar")
        self.assertEqual([a["text"].strip() for a in tabs],
                         ["floor", "goals", "tickets", "brain", "more"])
        self.assertEqual([a["text"] for a in tabs if a["attrs"].get("aria-current") == "page"],
                         ["tickets"])
        more = tabs[-1]["attrs"]
        self.assertEqual(more["href"], "#more")
        self.assertEqual(more["aria-controls"], "more")
        sheet = [a for a in links(elements, "sheet-panel")]
        self.assertEqual({a["text"].strip() for a in sheet} | {"Floor", "Goals", "Tickets", "Brain"},
                         set(LABELS))
        self.assertTrue(any(e["attrs"].get("id") == "more" for e in elements))

    def test_a_view_in_the_more_sheet_marks_more_and_its_sheet_row(self):
        elements = parse(render("needs-you"))
        more = links(elements, "tabbar")[-1]
        self.assertIn("on", more["attrs"]["class"].split())
        current = [a["text"] for a in links(elements, "sheet-panel")
                   if a["attrs"].get("aria-current") == "page"]
        self.assertEqual(current, ["Needs you"])

    def test_the_document_carries_the_body_assets_and_escaped_strings(self):
        doc = shell.page("<x>", None, '<p class="probe">body</p>',
                         status=shell.Status("missing", "<no journal>"))
        self.assertTrue(doc.startswith("<!DOCTYPE html>"))
        self.assertIn('<p class="probe">body</p>', doc)
        self.assertIn("<title>&lt;x&gt; - loom</title>", doc)
        self.assertIn("&lt;no journal&gt;", doc)
        self.assertIn('href="/static/loom.css"', doc)
        self.assertIn('src="/static/loom.js"', doc)
        self.assertNotIn('aria-current="page"', doc)
        doc.encode("ascii")

    def test_the_tick_status_prints_its_word_beside_the_dot(self):
        for state, word in (("ok", "tick ok"), ("failed", "tick failed"),
                            ("missing", "no tick seen")):
            elements = parse(render(status=shell.Status(state, "14:30")))
            statuses = [e for e in elements if "status-" + state in e["attrs"].get("class", "")]
            self.assertEqual(len(statuses), 2, state)  # nav head and top bar
            for s in statuses:
                self.assertEqual(s["text"], word + " 14:30")

    def test_theme_is_pinned_from_the_cookie_or_left_to_the_system(self):
        self.assertEqual(shell.theme_from_cookie("a=1; loom_theme=light"), "light")
        self.assertIsNone(shell.theme_from_cookie("loom_theme=pink"))
        self.assertIsNone(shell.theme_from_cookie(None))
        self.assertIn('<html lang="en" data-theme="dark">', render(theme="dark"))
        self.assertIn('<html lang="en">', render())

    def test_unknown_view_status_or_theme_is_refused(self):
        with self.assertRaises(ValueError):
            render("hot-spots")
        with self.assertRaises(ValueError):
            render(status=shell.Status("green"))
        with self.assertRaises(ValueError):
            render(theme="sepia")

    def test_counts_show_beside_their_view(self):
        nav = links(parse(render(counts={"needs-you": 3})), "nav-list")
        self.assertEqual(nav[-1]["text"], "Needs you3")


# CSS parsing: comments out, then the innermost `selector { declarations }`
# blocks, which skips @media wrappers and keeps the rules inside them.

def rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = {}
        for d in body.split(";"):
            name, _, value = d.partition(":")
            if value:
                decls[name.strip()] = value.strip()
        out.append(([s.strip() for s in sel.split(",")], decls))
    return out


def root_tokens(css):
    tokens = {}
    for selectors, decls in rules(css):
        if selectors == [":root"]:
            tokens.update({k: v for k, v in decls.items() if k.startswith("--")})
    return tokens


def split_args(text):
    """light-dark(A, B) -> (A, B), with commas inside A or B left alone."""
    depth, parts, start = 0, [], 0
    for i, ch in enumerate(text):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def linear_rgb(colour):
    """Linear-light sRGB channels of a #rrggbb or oklch(L C H) colour."""
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", colour)
    if m:
        chans = [int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        return [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    m = re.fullmatch(r"oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)", colour)
    if not m:
        raise ValueError("cannot read colour " + colour)
    lightness, chroma, hue = (float(g) for g in m.groups())
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return [4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
            -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
            -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_]


def luminance(colour):
    r, g, b = linear_rgb(colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def themes(tokens):
    """{"light": {token: colour}, "dark": {...}} for every light-dark() token."""
    out = {"light": {}, "dark": {}}
    for name, value in tokens.items():
        m = re.fullmatch(r"light-dark\((.*)\)", value)
        if m:
            light, dark = split_args(m.group(1))
            out["light"][name], out["dark"][name] = light, dark
    return out


ACCENTS = ("--amber", "--green", "--red", "--blue")
BODY_RATIO = 4.5  # WCAG AA for body text; large text needs 3:1


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.themes = themes(root_tokens(CSS))

    def test_every_text_token_clears_aa_on_every_background_in_both_themes(self):
        # Every text token (and each accent, which also colours state words)
        # is used at body size somewhere, so each is held to 4.5:1, which
        # also clears the 3:1 that large text needs.
        failures = []
        for theme, colours in self.themes.items():
            texts = [t for t in colours if t.startswith("--fg")] + list(ACCENTS)
            backgrounds = [t for t in colours if t.startswith("--bg")]
            self.assertGreaterEqual(len(texts), 10, theme)
            self.assertGreaterEqual(len(backgrounds), 6, theme)
            pairs = [(t, b) for t in texts for b in backgrounds] + [("--on-amber", "--amber")]
            for text, bg in pairs:
                ratio = contrast(colours[text], colours[bg])
                if ratio < BODY_RATIO:
                    failures.append("%s %s on %s: %.2f" % (theme, text, bg, ratio))
        self.assertEqual(failures, [])

    def test_the_two_themes_really_differ(self):
        light, dark = self.themes["light"], self.themes["dark"]
        self.assertLess(luminance(dark["--bg"]), 0.02)
        self.assertGreater(luminance(light["--bg"]), 0.8)

    def test_accents_are_inside_the_srgb_gamut(self):
        # Out-of-gamut colours get mapped by the browser, so the ratio above
        # would not be the ratio on screen.
        for theme, colours in self.themes.items():
            for token in ACCENTS:
                for channel in linear_rgb(colours[token]):
                    self.assertTrue(-0.001 <= channel <= 1.001, (theme, token, channel))

    def test_a_manual_theme_overrides_the_system_one(self):
        selectors = {s for sel, decls in rules(CSS) if "color-scheme" in decls for s in sel}
        self.assertTrue({':root[data-theme="dark"]', ':root[data-theme="light"]'} <= selectors)

    def test_type_is_geist_for_sentences_and_geist_mono_for_facts(self):
        tokens = root_tokens(CSS)
        self.assertTrue(tokens["--sans"].startswith('"Geist"'))
        self.assertTrue(tokens["--mono"].startswith('"Geist Mono"'))

    def test_no_shadows_and_no_motion(self):
        css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        self.assertEqual(re.findall(r"box-shadow|text-shadow|transition|animation", css), [])


# Interactive elements in the shell and the shared components.
INTERACTIVE = (".skip", ".brand", ".nav-link", ".tabbar-link", ".sheet-link", ".row",
               ".chip", ".segment", ".btn")
TAP = 44


def px(value, tokens):
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        value = tokens[m.group(1)]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", value)
    if not m:
        raise ValueError("not a px length: " + value)
    return float(m.group(1))


class TapTargetTests(unittest.TestCase):
    def test_interactive_elements_are_at_least_44px_both_ways(self):
        tokens = root_tokens(CSS)
        for cls in INTERACTIVE:
            found = [d for sel, d in rules(CSS) if cls in sel]
            self.assertTrue(found, cls)
            for prop in ("min-height", "min-width"):
                sizes = [px(d[prop], tokens) for d in found if prop in d]
                self.assertTrue(sizes, "%s has no %s" % (cls, prop))
                self.assertGreaterEqual(min(sizes), TAP, (cls, prop))

    def test_sheet_rows_are_at_least_52px(self):
        sizes = [d["min-height"] for sel, d in rules(CSS) if ".sheet-link" in sel and "min-height" in d]
        self.assertEqual(sizes, ["52px"])

    def test_every_link_and_button_in_the_shell_is_one_of_them(self):
        names = {c.lstrip(".") for c in INTERACTIVE}
        for e in parse(render()):
            if e["tag"] in ("a", "button") and e["attrs"].get("aria-hidden") != "true":
                self.assertTrue(set(e["attrs"].get("class", "").split()) & names, e)


class ScriptTests(unittest.TestCase):
    def test_one_small_vanilla_file(self):
        self.assertLess(len(JS.splitlines()), 300)
        self.assertNotRegex(JS, r"\bimport\b|\brequire\(")
        JS.encode("ascii")
        CSS.encode("ascii")

    def test_it_offers_poll_that_pauses_while_hidden(self):
        self.assertIn("function poll(url, every, onData", JS)
        self.assertIn("document.hidden", JS)
        self.assertIn("visibilitychange", JS)
        self.assertIn("window.loom = { poll: poll }", JS)

    def test_the_theme_cookie_name_matches_the_server(self):
        self.assertIn('THEME_COOKIE = "%s"' % shell.THEME_COOKIE, JS)


if __name__ == "__main__":
    unittest.main()
