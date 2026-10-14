import re
from html.parser import HTMLParser
from pathlib import Path

from dashboard.shell import VIEWS, page


ROOT = Path(__file__).parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.nav_labels = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            self.links.append(attributes)
            self.current.append([])
        if tag == "nav":
            self.nav_labels.append([])

    def handle_data(self, data):
        if self.current:
            self.current[-1].append(data)


def test_page_renders_all_views_as_links_with_one_active_view():
    html = page("Floor", "floor", "<p>Content</p>", status="OK")
    parser = Links()
    parser.feed(html)
    hrefs = {link["href"] for link in parser.links}
    assert {href for _, _, href in VIEWS} <= hrefs
    active = [link for link in parser.links if link.get("aria-current") == "page"]
    assert len(active) == 2  # Desktop navigation and the compact mobile tabs.
    assert all(link["href"] == "/" for link in active)
    assert 'class="mobile-tabs"' in html
    assert 'data-more-open' in html


def test_theme_toggle_names_the_theme_it_switches_to_and_the_current_one():
    html = page("Floor", "floor", "<p>Content</p>", status="OK")
    buttons = re.findall(r'<button class="theme-toggle"[^>]*aria-label="([^"]+)"[^>]*>([^<]+)</button>', html)
    assert len(buttons) == 2  # Desktop sidebar and mobile top bar.
    for label, text in buttons:
        assert text in ("Light", "Dark")
        assert label.startswith(text)  # The accessible name contains the visible word.
        assert "theme is on" in label


def _blocks(css):
    dark = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    light = re.search(r"@media \(prefers-color-scheme: light\)\s*\{\s*:root[^\{]*\{(.*?)\n  \}", css, re.S).group(1)
    return dark, light


def _vars(block):
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))


def _resolve(value, variables):
    while "var(" in value:
        key = re.search(r"var\(--([\w-]+)\)", value).group(1)
        value = variables[key]
    return value.strip()


def _luminance(hex_color):
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4 for part in channels]
    return sum(weight * part for weight, part in zip((.2126, .7152, .0722), linear, strict=True))


def _contrast(foreground, background):
    first, second = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (first + .05) / (second + .05)


def test_text_background_token_pairs_meet_wcag_aa_in_both_themes():
    css = (ROOT / "dashboard/static/loom.css").read_text()
    dark_block, light_block = _blocks(css)
    pairs = {
        "text-on-bg": "bg", "body-on-bg": "bg", "muted-on-bg": "bg",
        "dim-on-bg": "bg", "text-on-surface": "surface", "body-on-row": "row",
        "muted-on-raised": "raised", "accent-on-bg": "bg", "good-on-bg": "bg",
        "bad-on-bg": "bg", "info-on-bg": "bg",
    }
    dark_vars = _vars(dark_block)
    for block in (dark_block, light_block):
        variables = {**dark_vars, **_vars(block)}
        for foreground, background in pairs.items():
            ratio = _contrast(_resolve(variables[foreground], variables), _resolve(variables[background], variables))
            assert ratio >= 4.5, f"{foreground} on {background} is {ratio:.2f}:1"


def test_shared_interactive_components_have_44px_minimum_targets():
    css = (ROOT / "dashboard/static/loom.css").read_text()
    for selector in (".nav-row", ".chip", ".filter-chip", ".segment-tab", ".theme-toggle", ".icon-button", ".tab-link"):
        declarations = [
            body for selectors, body in re.findall(r"([^{}]+)\{([^{}]+)\}", css)
            if selector in [part.strip() for part in selectors.split(",")]
        ]
        assert declarations, f"missing CSS rule for {selector}"
        assert any("min-height: 44px" in body for body in declarations), f"{selector} is too short"
        # No later or more specific rule (a media query, a descendant selector) may shrink it again.
        overrides = [
            body for selectors, body in re.findall(r"([^{}]+)\{([^{}]+)\}", css)
            if any(part.strip().split()[-1:] == [selector] for part in selectors.split(","))
        ]
        for body in overrides:
            for prop, size in re.findall(r"(?<![\w-])(min-height|height|min-width|width):\s*(\d+)px", body):
                assert int(size) >= 44, f"{selector} has {prop}: {size}px"
