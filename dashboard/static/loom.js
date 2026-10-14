/* loom.js: the one script every dashboard screen shares.

   Every view state has a URL. Chips and tabs are real links to that URL, so
   each works without this script; with it they update the query in place.

   - Theme: [data-theme-toggle] buttons flip <html data-theme> and store the
     choice in the loom_theme cookie, which the server reads on the next page.
   - Filter chips: <a class="chip" data-param="state" data-value="pr"
     href="?state=pr">. When the page holds a [data-filter-list], the chips
     filter its [data-item] children client-side (an item matches when its
     data-<param> attribute lists the value) and push the new query; an item
     with no match shows the list's [data-empty]. Without a list, chips are
     plain links and the server filters. [data-clear] drops every filter.
   - Tabs: [data-tabs="name"] holding [role=tab] links with data-value and
     aria-controls. A click shows that pane and replaces ?name=value.
   - More sheet: [data-sheet="id"] opens #id; Escape, the backdrop or the
     opener closes it.
   - loom.poll(url, everyMs, onData, onError): fetch JSON every everyMs,
     paused while the tab is hidden and fetched at once when it is shown
     again. Returns a function that stops it. */
(function () {
  "use strict";

  var THEME_COOKIE = "loom_theme";
  var root = document.documentElement;
  root.classList.add("js");

  /* Theme */

  function cookie(name) {
    var parts = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < parts.length; i++) {
      var kv = parts[i].trim().split("=");
      if (kv[0] === name) return decodeURIComponent(kv.slice(1).join("="));
    }
    return null;
  }

  function theme() {
    var pinned = root.getAttribute("data-theme");
    if (pinned === "light" || pinned === "dark") return pinned;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function labelToggles() {
    var now = theme();
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.hidden = false;
      var name = b.querySelector("[data-theme-name]");
      if (name) name.textContent = now;
      b.setAttribute("aria-label", "Theme: " + now + ". Switch to " + (now === "light" ? "dark" : "light"));
    });
  }

  function toggleTheme() {
    var next = theme() === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    document.cookie = THEME_COOKIE + "=" + next + "; path=/; max-age=31536000; samesite=lax";
    labelToggles();
  }

  var stored = cookie(THEME_COOKIE);
  if (stored === "light" || stored === "dark") root.setAttribute("data-theme", stored);

  /* URL state */

  function query() {
    return new URLSearchParams(window.location.search);
  }

  function urlFor(params) {
    var q = params.toString();
    return window.location.pathname + (q ? "?" + q : "");
  }

  /* Filter chips */

  function filterParams() {
    var names = [];
    document.querySelectorAll("[data-param]").forEach(function (chip) {
      var name = chip.getAttribute("data-param");
      if (names.indexOf(name) < 0) names.push(name);
    });
    return names;
  }

  function matches(item, params, names) {
    return names.every(function (name) {
      var want = params.get(name);
      if (!want) return true;
      var have = (item.getAttribute("data-" + name) || "").split(/\s+/);
      return have.indexOf(want) >= 0;
    });
  }

  function applyFilters() {
    var params = query(), names = filterParams();
    document.querySelectorAll("[data-param]").forEach(function (chip) {
      var want = params.get(chip.getAttribute("data-param")) || "";
      var on = want === (chip.getAttribute("data-value") || "");
      if (on) chip.setAttribute("aria-current", "true");
      else chip.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-filter-list]").forEach(function (list) {
      var shown = 0;
      list.querySelectorAll("[data-item]").forEach(function (item) {
        var ok = matches(item, params, names);
        item.hidden = !ok;
        if (ok) shown++;
      });
      var empty = list.querySelector("[data-empty]");
      if (empty) empty.hidden = shown > 0;
    });
  }

  function onChip(chip, event) {
    if (!document.querySelector("[data-filter-list]")) return;
    event.preventDefault();
    var params = query();
    var name = chip.getAttribute("data-param");
    var value = chip.getAttribute("data-value") || "";
    if (!value || params.get(name) === value) params.delete(name);
    else params.set(name, value);
    window.history.pushState(null, "", urlFor(params));
    applyFilters();
  }

  function onClear(event) {
    if (!document.querySelector("[data-filter-list]")) return;
    event.preventDefault();
    var params = query();
    filterParams().forEach(function (name) { params.delete(name); });
    window.history.pushState(null, "", urlFor(params));
    applyFilters();
  }

  /* Tabs */

  function tabsOf(group) {
    return Array.prototype.slice.call(group.querySelectorAll("[role=tab]"));
  }

  function selectTab(group, value, focus) {
    var tabs = tabsOf(group);
    if (!tabs.length) return;
    var chosen = tabs.filter(function (t) { return t.getAttribute("data-value") === value; })[0];
    if (!chosen) {
      chosen = tabs.filter(function (t) { return t.getAttribute("aria-selected") === "true"; })[0] || tabs[0];
    }
    tabs.forEach(function (t) {
      var on = t === chosen;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.setAttribute("tabindex", on ? "0" : "-1");
      var pane = document.getElementById(t.getAttribute("aria-controls") || "");
      if (pane) pane.hidden = !on;
    });
    if (focus) chosen.focus();
  }

  function applyTabs() {
    var params = query();
    document.querySelectorAll("[data-tabs]").forEach(function (group) {
      selectTab(group, params.get(group.getAttribute("data-tabs")), false);
    });
  }

  function onTab(tab, event) {
    event.preventDefault();
    var group = tab.closest("[data-tabs]");
    var value = tab.getAttribute("data-value");
    var params = query();
    params.set(group.getAttribute("data-tabs"), value);
    window.history.replaceState(null, "", urlFor(params));
    selectTab(group, value, false);
  }

  function onTabKey(tab, event) {
    var step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    var group = tab.closest("[data-tabs]");
    var tabs = tabsOf(group);
    var next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
    event.preventDefault();
    onTab(next, event);
    next.focus();
  }

  /* More sheet */

  var openSheet = null;

  function setSheet(opener, open) {
    var sheet = document.getElementById(opener.getAttribute("data-sheet"));
    if (!sheet) return;
    sheet.classList.toggle("open", open);
    opener.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      openSheet = opener;
      var first = sheet.querySelector(".sheet-link");
      if (first) first.focus();
    } else {
      openSheet = null;
      if (window.location.hash === "#" + sheet.id) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
      opener.focus();
    }
  }

  /* Polling */

  function poll(url, every, onData, onError) {
    var timer = null, busy = false, stopped = false;

    function schedule() {
      clearTimeout(timer);
      timer = null;
      if (!stopped && !document.hidden) timer = setTimeout(tick, every);
    }

    function tick() {
      timer = null;
      if (stopped || document.hidden || busy) return;
      busy = true;
      fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error(url + ": HTTP " + r.status);
          return r.json();
        })
        .then(function (data) { if (!stopped) onData(data); })
        .catch(function (err) {
          if (onError) onError(err);
          else console.warn("loom.poll", err);
        })
        .then(function () { busy = false; schedule(); });
    }

    function onVisibility() {
      if (document.hidden) {
        clearTimeout(timer);
        timer = null;
      } else if (!timer && !busy) {
        tick();
      }
    }

    document.addEventListener("visibilitychange", onVisibility);
    schedule();
    return function stop() {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }

  /* Wiring */

  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-theme-toggle], [data-param], [data-clear], [data-tabs] [role=tab], [data-sheet], [data-sheet-close]");
    if (!el || event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
    if (el.hasAttribute("data-theme-toggle")) toggleTheme();
    else if (el.hasAttribute("data-param")) onChip(el, event);
    else if (el.hasAttribute("data-clear")) onClear(event);
    else if (el.getAttribute("role") === "tab") onTab(el, event);
    else if (el.hasAttribute("data-sheet")) {
      event.preventDefault();
      setSheet(el, el.getAttribute("aria-expanded") !== "true");
    } else if (openSheet) {
      event.preventDefault();
      setSheet(openSheet, false);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && openSheet) setSheet(openSheet, false);
    var tab = event.target.closest && event.target.closest("[data-tabs] [role=tab]");
    if (tab) onTabKey(tab, event);
  });

  window.addEventListener("popstate", function () {
    applyFilters();
    applyTabs();
  });

  labelToggles();
  applyFilters();
  applyTabs();
  document.querySelectorAll("[data-sheet]").forEach(function (opener) {
    if (window.location.hash === "#" + opener.getAttribute("data-sheet")) setSheet(opener, true);
  });

  window.loom = { poll: poll };
})();
