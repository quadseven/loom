(() => {
  const sheet = document.querySelector('#more-sheet');
  const backdrop = document.querySelector('.sheet-backdrop');
  const opener = document.querySelector('[data-more-open]');
  const closeButtons = document.querySelectorAll('[data-more-close]');

  function setSheet(open) {
    if (!sheet || !backdrop) return;
    sheet.hidden = !open;
    backdrop.hidden = !open;
    if (opener) opener.setAttribute('aria-expanded', String(open));
    document.body.style.overflow = open ? 'hidden' : '';
    if (open) sheet.querySelector('button, a')?.focus();
    else opener?.focus();
  }

  opener?.addEventListener('click', () => setSheet(true));
  closeButtons.forEach((button) => button.addEventListener('click', () => setSheet(false)));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && sheet && !sheet.hidden) setSheet(false);
  });

  const themeButtons = document.querySelectorAll('[data-theme-toggle]');

  function currentTheme() {
    return document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  }

  // Each button names the theme it switches to; its aria-label also names the one showing.
  function syncThemeButtons() {
    const current = currentTheme();
    const next = current === 'dark' ? 'light' : 'dark';
    const word = next === 'dark' ? 'Dark' : 'Light';
    themeButtons.forEach((button) => {
      button.textContent = word;
      button.setAttribute('aria-label', `${word} theme (${current} theme is on)`);
    });
  }

  function saveTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.cookie = `loom-theme=${theme}; Path=/; Max-Age=31536000; SameSite=Lax`;
    syncThemeButtons();
  }

  const savedTheme = document.cookie.split('; ').find((part) => part.startsWith('loom-theme='));
  if (savedTheme) document.documentElement.dataset.theme = savedTheme.split('=')[1];
  syncThemeButtons();
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', syncThemeButtons);
  themeButtons.forEach((button) => {
    button.addEventListener('click', () => saveTheme(currentTheme() === 'dark' ? 'light' : 'dark'));
  });

  function updateQuery(name, value) {
    const url = new URL(window.location.href);
    if (value === null || value === '') url.searchParams.delete(name);
    else url.searchParams.set(name, value);
    history.pushState({}, '', url);
    syncQueryControls();
  }

  function syncQueryControls() {
    const params = new URL(window.location.href).searchParams;
    document.querySelectorAll('[data-filter-name]').forEach((control) => {
      const selected = params.get(control.dataset.filterName) === control.dataset.filterValue;
      control.setAttribute('aria-pressed', String(selected));
      control.classList.toggle('is-active', selected);
    });
    document.querySelectorAll('[data-query-tab]').forEach((control) => {
      const name = control.dataset.queryName || 'tab';
      const selected = params.get(name) === control.dataset.queryTab;
      control.setAttribute('aria-selected', String(selected));
      control.classList.toggle('is-active', selected);
    });
  }

  window.addEventListener('popstate', syncQueryControls);
  syncQueryControls();

  document.addEventListener('click', (event) => {
    const filter = event.target.closest('[data-filter-name]');
    if (filter) {
      event.preventDefault();
      const selected = filter.getAttribute('aria-pressed') === 'true';
      updateQuery(filter.dataset.filterName, selected ? null : filter.dataset.filterValue);
      return;
    }
    const tab = event.target.closest('[data-query-tab]');
    if (tab) {
      const name = tab.dataset.queryName || 'tab';
      updateQuery(name, tab.dataset.queryTab);
    }
  });

  window.loomPoll = function poll(url, every, onData) {
    let timer = null;
    let stopped = false;
    let running = false;
    const delay = Math.max(1000, Number(every) || 5000);

    async function tick() {
      if (stopped || document.hidden || running) return;
      running = true;
      try {
        const response = await fetch(typeof url === 'function' ? url() : url, { headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error(`Poll failed: ${response.status}`);
        onData(await response.json());
      } catch (error) {
        // A later poll retries transient network failures.
        console.error(error);
      } finally {
        running = false;
      }
    }

    function schedule() {
      if (timer) clearInterval(timer);
      timer = null;
      if (!stopped && !document.hidden) {
        tick();
        timer = setInterval(tick, delay);
      }
    }

    document.addEventListener('visibilitychange', schedule);
    schedule();
    return () => {
      stopped = true;
      if (timer) clearInterval(timer);
      document.removeEventListener('visibilitychange', schedule);
    };
  };

  const thoughtToggle = document.querySelector('[data-thought-toggle]');
  thoughtToggle?.addEventListener('change', () => {
    document.querySelectorAll('[data-thought]').forEach((row) => { row.hidden = !thoughtToggle.checked; });
  });
  document.querySelectorAll('[data-ticket-tab]').forEach((tab) => {
    tab.addEventListener('click', (event) => {
      if (window.matchMedia('(max-width: 959px)').matches) {
        event.preventDefault();
        document.querySelectorAll('[data-pane]').forEach((pane) => {
          pane.hidden = pane.dataset.pane !== tab.dataset.ticketTab;
        });
        document.querySelectorAll('[data-ticket-tab]').forEach((item) => item.setAttribute('aria-current', String(item === tab)));
        updateQuery('tab', tab.dataset.ticketTab);
      }
    });
  });
  const initialTab = new URL(window.location.href).searchParams.get('tab') || 'transcript';
  if (window.matchMedia('(max-width: 959px)').matches) {
    document.querySelectorAll('[data-pane]').forEach((pane) => { pane.hidden = pane.dataset.pane !== initialTab; });
  }
  const liveRun = document.querySelector('.run-events[data-running="true"][data-attempt]');
  if (liveRun && liveRun.dataset.attempt) {
    let after = '';
    const ticketPath = window.location.pathname.split('/').slice(2).join('/');
    const eventsUrl = () => `/api/tickets/${ticketPath}/runs/${encodeURIComponent(liveRun.dataset.attempt)}${after ? `?after=${encodeURIComponent(after)}` : ''}`;
    const liveRow = liveRun.querySelector('.transcript-event.live');
    const labelOf = (event) => event.role === 'user' ? 'brief' : event.role === 'thinking' ? 'thought'
      : event.kind === 'tool_use' ? 'ran' : event.kind === 'tool_result' ? 'output'
      : event.role === 'runner' ? 'runner' : 'said';
    window.loomPoll(eventsUrl, 5000, (payload) => {
      (payload.events || []).forEach((event) => {
        const label = labelOf(event);
        const row = document.createElement('div');
        row.className = `transcript-event ${label}`;
        if (label === 'thought') {
          row.dataset.thought = '';
          row.hidden = thoughtToggle ? !thoughtToggle.checked : false;
        }
        const head = document.createElement('div'); head.className = 'tx-head';
        const name = document.createElement('span'); name.className = 'tx-label'; name.textContent = label;
        const at = document.createElement('span'); at.className = 'tx-at'; at.textContent = (event.ts || '').slice(11, 16);
        head.append(name, at);
        const content = document.createElement(label === 'said' || label === 'brief' ? 'p' : label === 'thought' ? 'em' : 'pre');
        if (content.tagName === 'PRE') content.className = label === 'runner' ? 'runner' : 'tool-block';
        content.textContent = event.text || '';
        row.append(head, content);
        liveRun.insertBefore(row, liveRow);
        after = event.ts || after;
      });
    });
  }
})();
