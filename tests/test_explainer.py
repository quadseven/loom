"""Safety and accessibility checks for the public, static explainer."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "dashboard" / "explainer.html"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []
        self.text = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "style":
            self.in_style = True
        self.tags.append((tag, attrs))
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if not self.in_style:
            self.text.append(data)


def rgb(value):
    value = value.strip().lower()
    if value.startswith("var("):
        raise AssertionError("resolve variables before calculating contrast")
    if not re.fullmatch(r"#[0-9a-f]{6}", value):
        raise AssertionError("unsupported color syntax: " + value)
    return tuple(int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))


def luminance(value):
    channels = []
    for channel in rgb(value):
        channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(foreground, background):
    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)


class ExplainerPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PAGE.read_text(encoding="utf-8")
        cls.parser = PageParser()
        cls.parser.feed(cls.source)
        cls.css = re.search(r"<style>(.*?)</style>", cls.source, re.S).group(1)

    def test_links_and_copy_contain_no_private_identifiers(self):
        for href in self.parser.links:
            parsed = urlparse(href)
            if parsed.scheme or parsed.netloc:
                self.assertIn(parsed.hostname, {"github.com", "fonts.googleapis.com"}, href)
                if parsed.hostname == "github.com":
                    self.assertRegex(parsed.path, r"^/quadseven/loom(?:/|$)", href)
        all_text = self.source + "\n" + " ".join(self.parser.text)
        self.assertIsNone(re.search(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", all_text))
        # Two adjacent capitalized words in visible copy are a likely person name.
        visible = " ".join(self.parser.text)
        likely_names = set(re.findall(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", visible))
        page_phrases = {"Brain Measures", "Clone Loom", "Goal Describe", "Repository Docs", "Repository Glossary", "Review Automated", "Ticket States"}
        self.assertEqual(likely_names - page_phrases, set())
        self.assertNotRegex(all_text, r"github\.com/(?!quadseven/loom(?:/|\b))[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

    def test_declared_text_and_surface_pairs_meet_wcag_aa(self):
        variables = dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", self.css))
        rules = {}
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", self.css):
            declarations = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", body))
            rules[selector.strip()] = declarations

        pairs = {
            "body": ("--text", "--bg"),
            "p": ("--muted", "--bg"),
            ".example span": ("--soft", "--surface"),
            ".chip": ("--body", "--raised"),
            ".step-number": ("--soft", "--bg"),
            ".ticket-id": ("--soft", "--surface"),
            ".event": ("--body", "--surface"),
            ".event time": ("--blue", "--surface"),
            ".event:nth-child(2) time,.event:nth-child(3) time": ("--red", "--surface"),
            ".event:last-child time": ("--green", "--surface"),
            ".lane": ("--muted", "--bg"),
            ".lane b": ("--amber", "--bg"),
            ".moved .eyebrow": ("--green", "--surface"),
            ".rule": ("--text", "--surface"),
            "pre": ("--body", "--code"),
            "footer": ("--soft", "--bg"),
            ".primary": ("--bg", "--amber"),
            ".secondary": ("--text", "--bg"),
            "a": ("--amber", "--bg"),
        }
        for selector, (foreground, background) in pairs.items():
            with self.subTest(selector=selector):
                self.assertIn(selector, rules)
                self.assertIn(foreground, variables)
                self.assertIn(background, variables)
                self.assertGreaterEqual(contrast(variables[foreground], variables[background]), 4.5)

    def test_every_link_and_button_has_a_44px_css_tap_target(self):
        rules = {}
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", self.css):
            declarations = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", body))
            for selector in selectors.split(","):
                rules[selector.strip()] = declarations

        for tag, attrs in self.parser.tags:
            if tag in {"a", "button"}:
                declarations = dict(rules.get(tag, {}))
                for class_name in attrs.get("class", "").split():
                    declarations.update(rules.get("." + class_name, {}))
                height = declarations.get("min-height", declarations.get("height"))
                self.assertIsNotNone(height, f"{tag} has no declared tap target height")
                self.assertGreaterEqual(float(height.removesuffix("px")), 44, f"{tag} tap target too small")


if __name__ == "__main__":
    unittest.main()
