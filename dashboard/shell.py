"""HTML document shell shared by dashboard views."""

from html import escape


VIEWS = (
    ("floor", "Floor", "/"),
    ("backlog", "Backlog", "/backlog"),
    ("goals", "Goals", "/goals"),
    ("tree", "Tree", "/tree"),
    ("tickets", "Tickets", "/tickets"),
    ("chain", "Command chain", "/chain"),
    ("brain", "Brain", "/brain"),
    ("hot-spots", "Hot spots", "/hot-spots"),
    ("results", "Results", "/results"),
    ("improving", "Improving", "/improving"),
    ("lanes", "Lanes and models", "/lanes"),
    ("usage", "Usage", "/usage"),
    ("tiers", "Tiers and allowances", "/tiers"),
    ("operators", "Operators", "/operators"),
    ("needs-you", "Needs you", "/needs-you"),
    ("free", "Free to take", "/free"),
)
MOBILE_TABS = ("floor", "goals", "tickets", "brain")


def _link(key: str, label: str, href: str, active: str, extra: str = "") -> str:
    current = ' aria-current="page"' if active == key else ""
    return (
        f'<a class="nav-row {extra}" href="{escape(href, quote=True)}"'
        f'{current}>{escape(label)}</a>'
    )


def page(title: str, active: str, body: str, *, status: str) -> str:
    """Render a complete responsive dashboard page.

    ``body`` is trusted view markup; all shell-provided text and attributes are escaped.
    """
    title_text = escape(title)
    status_text = escape(status)
    nav = "\n".join(_link(key, label, href, active) for key, label, href in VIEWS)
    tabs = "\n".join(
        _link(key, next(label for slug, label, _ in VIEWS if slug == key), href, active, "tab-link")
        for key, _, href in VIEWS if key in MOBILE_TABS
    )
    more = "\n".join(
        _link(key, label, href, active)
        for key, label, href in VIEWS if key not in MOBILE_TABS
    )
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>{title_text} - Loom</title>
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
  <link rel="stylesheet" href="/static/loom.css">
  <script src="/static/loom.js" defer></script>
</head>
<body>
  <aside class="desktop-nav" aria-label="Main navigation">
    <a class="brand" href="/">loom.</a>
    <nav>{nav}</nav>
    <div class="tick-status"><span class="status-dot" aria-hidden="true"></span><span>Last tick</span><strong>{status_text}</strong></div>
    <button class="theme-toggle" type="button" data-theme-toggle aria-label="Light theme (dark theme is on)">Light</button>
  </aside>
  <header class="mobile-topbar">
    <a class="brand" href="/">loom.</a>
    <span class="tick-status"><span class="status-dot" aria-hidden="true"></span><span>Last tick</span><strong>{status_text}</strong></span>
    <button class="theme-toggle" type="button" data-theme-toggle aria-label="Light theme (dark theme is on)">Light</button>
  </header>
  <main class="page-content">
    <h1 class="page-title">{title_text}</h1>
    {body}
  </main>
  <nav class="mobile-tabs" aria-label="Primary navigation">
    {tabs}
    <button class="tab-link" type="button" data-more-open aria-controls="more-sheet" aria-expanded="false">More</button>
  </nav>
  <div class="sheet-backdrop" data-more-close hidden></div>
  <section class="more-sheet" id="more-sheet" role="dialog" aria-modal="true" aria-label="More views" hidden>
    <div class="sheet-handle" aria-hidden="true"></div>
    <div class="sheet-heading"><h2>More views</h2><button type="button" class="icon-button" data-more-close aria-label="Close menu">Close</button></div>
    <nav aria-label="More navigation">{more}</nav>
  </section>
</body>
</html>'''
