#!/usr/bin/env python3
"""Usage website watcher - Claude / Codex subscription dashboards.

There is no usage API for these subscriptions, so Loom watches the
official usage websites instead. A scheduled, logged-in browser check
opens the owner's own account usage pages on the official sites and reads
the numbers into state/usage/<lane>.json.

ToS-clean by construction: the owner's own dashboard, the official site,
just looking. No scraping of other people's data, no credential sharing,
no API abuse. If a page structure changes, this writer logs the miss and
the dispatcher falls back to worker self-reported burn until it is fixed.
"""
