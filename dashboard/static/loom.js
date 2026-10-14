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

  function saveTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.cookie = `loom-theme=${theme}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }

  const savedTheme = document.cookie.split('; ').find((part) => part.startsWith('loom-theme='));
  if (savedTheme) document.documentElement.dataset.theme = savedTheme.split('=')[1];
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const current = document.documentElement.dataset.theme
        || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      saveTheme(current === 'dark' ? 'light' : 'dark');
    });
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
        const response = await fetch(url, { headers: { Accept: 'application/json' } });
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
})();
