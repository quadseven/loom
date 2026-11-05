// Invented sample data for the Loom prototype. Shapes mirror what the real
// bus can serve: ticket labels (bus/labels.py), transcripts (bus/transcripts.py:
// {ticket, slot, model, outcome, started_at, finished_at, events:[{ts, role, kind, text}]}),
// journals/reports (docs/MUSE.md), lane heartbeats (loom:lanes issue).

export const NOW = '2026-10-14T14:38:00Z';
export const STATES = ['queued', 'briefed', 'running', 'pr', 'merged', 'shipped', 'verified'];

export const standup = {
  at: '2026-10-14T12:30:00Z',
  line: 'Better since the last report: 6 tickets verified, and median ticket-to-verified fell from 9.2h to 7.4h.',
  well: ['Briefs now name the failing test, so first-pass clean PRs rose to 58%.', 'grug#1146 finally merged on rung 2 after 15 attempts.'],
  notWell: ['assay p95 regressed after assay#38 shipped (412 to 588ms).', 'anthropic-3 is out of credit until 18:00.'],
  stuck: ['loom#201 lane heartbeats, past the last rung for 9h.'],
  next: 'Fix the assay regression first, then fill the 5 free lanes.',
  needsYou: 'Top up Anthropic credit or let 3 hard tickets move to codex.',
};

export const factory = [
  { k: 'verified, 7d', v: '41', prev: '33', good: true },
  { k: 'ticket to verified', v: '7.4h', prev: '9.2h', good: true },
  { k: 'first-pass clean', v: '58%', prev: '41%', good: true },
  { k: 'fix rounds / PR', v: '1.3', prev: '1.9', good: true },
  { k: 'ladder climbs', v: '9', prev: '6', good: false },
  { k: 'anthropic share', v: '32%', prev: '38%', good: true, cap: '50%' },
  { k: 'lane idle', v: '18%', prev: '22%', good: true },
  { k: 'reverts', v: '1', prev: '0', good: false },
  { k: 'asks open', v: '2', prev: '3', good: true },
];

export const dwell = [
  { state: 'queued', median: 0.4, worst: 2.1 },
  { state: 'briefed', median: 0.6, worst: 3.0 },
  { state: 'running', median: 1.1, worst: 4.8 },
  { state: 'pr', median: 3.1, worst: 14.2, note: '60% of it waiting on fix rounds' },
  { state: 'merged', median: 0.5, worst: 1.9 },
  { state: 'shipped', median: 1.6, worst: 6.0, note: 'waiting for live proof' },
];

export const goals = [
  {
    id: 'quadseven/loom#30', short: 'loom', title: 'Loom drives every goal to verified, cheaply',
    metric: { name: 'median ticket to verified', value: '7.4h', prev: '9.2h', better: 'down', trend: [12.1, 11.4, 10.8, 9.9, 9.2, 8.1, 7.4] },
    cap: 8, counts: { queued: 1, briefed: 0, running: 2, pr: 1, merged: 1, shipped: 1, verified: 2, failed: 1 },
    report: 'Better: 4 of the factory numbers moved the right way, and the hand-back of review findings is running.',
  },
  {
    id: 'quadseven/grug#1143', short: 'grug', title: "grug's reviews are right the first time",
    metric: { name: 'findings overturned', value: '6%', prev: '11%', better: 'down', trend: [18, 15, 14, 11, 11, 8, 6] },
    cap: 4, counts: { queued: 1, briefed: 0, running: 0, pr: 0, merged: 1, shipped: 0, verified: 2, failed: 0 },
    report: 'Better: Refs across repos now resolve, so reviews read the linked ticket.',
  },
  {
    id: 'quadseven/assay#1', short: 'assay', title: 'assay scores every lane on real tickets',
    metric: { name: 'p95 benchmark run', value: '588ms', prev: '412ms', better: 'down', trend: [430, 425, 418, 412, 412, 590, 588], regression: true },
    cap: 4, counts: { queued: 0, briefed: 1, running: 1, pr: 1, merged: 0, shipped: 0, verified: 2, failed: 0 },
    report: 'Worse: p95 jumped after assay#38 shipped. The fix takes the regression slot.',
  },
];

const T = (o) => ({ attempts: 1, rung: 0, ...o });
export const tickets = [
  T({ id: 'quadseven/loom#94', goal: 'quadseven/loom#30', title: 'Review findings go back to the authoring lane on their own', state: 'running', lane: 'anthropic-2', model: 'claude-opus-5-5', difficulty: 'hard', age: '41m', live: true }),
  T({ id: 'quadseven/assay#55', goal: 'quadseven/assay#1', title: 'The lanes suite scores spark separately', state: 'running', lane: 'anthropic', model: 'claude-opus-5-5', difficulty: 'hard', age: '12m' }),
  T({ id: 'quadseven/loom#233', goal: 'quadseven/loom#30', title: 'Each journal names what slowed the tick down', state: 'running', lane: 'anthropic-4', model: 'claude-opus-5-5', difficulty: 'medium', age: '3m' }),
  T({ id: 'quadseven/assay#41', goal: 'quadseven/assay#1', title: 'The benchmark reports p95 per lane, not only the mean', state: 'pr', sub: 'fixing', pr: 52, lane: 'opencode-go', model: 'kimi-k2.7-code', difficulty: 'medium', age: '2h', attempts: 2 }),
  T({ id: 'quadseven/loom#228', goal: 'quadseven/loom#30', title: 'Transient and limit errors retry the lane on their own', state: 'pr', sub: 'ready', pr: 236, lane: 'codex-2', model: 'gpt-6-luna', difficulty: 'medium', age: '1h' }),
  T({ id: 'quadseven/grug#1146', goal: 'quadseven/grug#1143', title: 'The review reads the ticket a lane PR links as Refs owner/name#N', state: 'merged', pr: 1203, lane: 'codex', model: 'gpt-6-sol', difficulty: 'easy', age: '6h', attempts: 15, rung: 2 }),
  T({ id: 'quadseven/loom#212', goal: 'quadseven/loom#30', title: "Brain status shows each ticket's rung history", state: 'merged', pr: 214, lane: 'codex', model: 'gpt-6-luna', difficulty: 'medium', age: '3h' }),
  T({ id: 'quadseven/loom#84', goal: 'quadseven/loom#30', title: 'Every two hours a report reaches the operator phone', state: 'shipped', pr: 190, lane: 'claude', model: 'claude-sonnet-5-5', difficulty: 'medium', age: '5h', sub: 'awaiting proof' }),
  T({ id: 'quadseven/assay#56', goal: 'quadseven/assay#1', title: 'p95 is back under 450ms after assay#38', state: 'briefed', lane: 'codex-2', model: 'gpt-6-luna', difficulty: 'medium', age: '8m', sub: 'regression slot', regression: true }),
  T({ id: 'quadseven/loom#234', goal: 'quadseven/loom#30', title: "The station moves spark's tickets while its box is hot", state: 'queued', difficulty: 'easy', age: '20m' }),
  T({ id: 'quadseven/grug#1170', goal: 'quadseven/grug#1143', title: 'The format check runs on pull requests from forks', state: 'queued', difficulty: 'easy', age: '1h' }),
  T({ id: 'quadseven/loom#201', goal: 'quadseven/loom#30', title: 'Lane heartbeats survive a laptop sleep', state: 'failed', sub: 'past the last rung', pr: 207, lane: 'anthropic-5', model: 'claude-opus-5-5', difficulty: 'medium', age: '9h', attempts: 8, rung: 3 }),
  T({ id: 'quadseven/loom#231', goal: 'quadseven/loom#30', title: 'Briefs name the failing test up front', state: 'verified', pr: 232, lane: 'claude', model: 'claude-sonnet-5-5', difficulty: 'medium', age: '1d' }),
  T({ id: 'quadseven/grug#1161', goal: 'quadseven/grug#1143', title: 'Settle the review findings left from PR #1152', state: 'verified', pr: 1166, lane: 'codex', model: 'gpt-6-sol', difficulty: 'easy', age: '1d', attempts: 9, rung: 1 }),
  T({ id: 'quadseven/grug#1162', goal: 'quadseven/grug#1143', title: "Haunt counts Elder's own Fixed-in reply as handled", state: 'verified', pr: 1167, lane: 'codex', model: 'gpt-6-luna', difficulty: 'easy', age: '1d' }),
  T({ id: 'quadseven/assay#38', goal: 'quadseven/assay#1', title: 'The coding benchmark can measure a vLLM-served model', state: 'verified', pr: 40, lane: 'codex', model: 'gpt-6-luna', difficulty: 'medium', age: '2d', note: 'caused the p95 regression' }),
  T({ id: 'quadseven/assay#39', goal: 'quadseven/assay#1', title: 'The unix-socket proxy test never flakes', state: 'verified', pr: 44, lane: 'opencode-go', model: 'muse-spark-1.3-contributor', difficulty: 'easy', age: '2d', attempts: 2 }),
];

// Detailed records for a few tickets. Others get generated paths in the app.
export const details = {
  'quadseven/grug#1146': {
    checks: [
      { done: true, text: 'test_refs_crossrepo fails without the fix' },
      { done: true, text: 'Refs owner/name#N anywhere in the body resolves' },
      { done: false, text: 'Live: the next lane PR review cites its linked ticket' },
    ],
    path: [
      { ts: '2026-10-14T09:02:00Z', who: 'brain', text: 'Briefed from grug#1143. Easy, so rung 0 goes to a free lane.', meta: 'opencode-go / kimi-k2.7-code' },
      { ts: '2026-10-14T09:14:00Z', who: 'lane', text: 'opencode-go opened PR #1180 on its own branch.' },
      { ts: '2026-10-14T09:31:00Z', who: 'grug', text: '3 findings, 1 high: Refs only parsed at the start of a line.', meta: 'handed back, fix round 1 of 3' },
      { ts: '2026-10-14T10:40:00Z', who: 'fail', text: 'Fix round 3 used. The high finding never closed.', meta: 'attempts 2 to 11' },
      { ts: '2026-10-14T11:50:00Z', who: 'fail', text: 'Repeatedly stuck: failed twice. The factory climbs the ladder.' },
      { ts: '2026-10-14T13:40:00Z', who: 'fail', text: 'Rung 1, claude-sonnet-5-5. CI red on test_refs_crossrepo.', meta: 'attempts 12 to 14' },
      { ts: '2026-10-14T15:05:00Z', who: 'lane', text: 'Rung 2, codex / gpt-6-sol. New PR #1203 replaces #1180.' },
      { ts: '2026-10-14T15:22:00Z', who: 'ok', text: 'grug: 0 open findings. CI green. Labelled loom:ready.' },
      { ts: '2026-10-14T15:30:00Z', who: 'brain', text: 'Merged by muse on head 9f2c1e4.' },
      { ts: null, who: 'next', text: 'Ship, then verify the live proof line.' },
    ],
    pr: {
      num: 1203, ci: [['unit', 'pass'], ['format, grug recheck', 'pass'], ['review, grug', '0 open'], ['deploy grug.ts', 'pending']],
      files: [['grug/review/context.py', 21, 6], ['tests/test_refs.py', 8, 2]],
      threads: [
        { ts: '2026-10-14T15:19:00Z', severity: 'medium', text: 'Refs inside fenced code blocks also match here. Is that intended?', answer: 'Not intended. Fenced blocks are now stripped before matching. Added test_refs_in_code_ignored.', resolved: true },
      ],
    },
    summary: 'Attempts 1 to 14 all anchored the match to the start of a line, and grug raised the same high finding 9 times. The brief template now names that case (loom#231).',
    runs: 'grug1146',
  },
  'quadseven/loom#94': {
    checks: [
      { done: true, text: 'test_unresolved_thread_hands_back fails without the fix' },
      { done: false, text: 'An unresolved grug thread reaches the lane within one station pass' },
      { done: false, text: 'Live: a lane PR is fixed with no brain fix command in its history' },
    ],
    path: [
      { ts: '2026-10-14T13:20:00Z', who: 'brain', text: 'Briefed from the 12:00 journal: brain sent findings back by hand 7 times today.', meta: 'hard, anthropic-2 / claude-opus-5-5' },
      { ts: '2026-10-14T13:57:00Z', who: 'lane', text: 'anthropic-2 claimed it from the station.' },
      { ts: null, who: 'live', text: 'Running for 41m.' },
    ],
    pr: null,
    runs: 'loom94',
  },
};

const ev = (ts, role, kind, text) => ({ ts: '2026-10-14T' + ts + ':00Z', role, kind, text });

export const transcripts = {
  loom94: [{
    ticket: 'quadseven/loom#94', slot: 'anthropic-2', model: 'claude-opus-5-5', outcome: 'running', started_at: '2026-10-14T13:57:00Z', finished_at: null,
    events: [
      ev('13:57', 'user', 'text', 'Why: the brain hands review findings back by hand with bus.brain fix, 7 times today.\nWhat: the station hands every unresolved review-bot thread and failed check back to the authoring lane, up to LOOM_FIX_ROUNDS.\nAcceptance criteria:\n- [ ] test_unresolved_thread_hands_back fails without the fix\n- [ ] an unresolved thread reaches the lane within one station pass\n- [ ] live: a lane PR is fixed with no brain fix command in its history\nSize: M\nOut of scope: threads a person opened.'),
      ev('14:02', 'thinking', 'text', 'The ticket wants findings handed back without the brain. The station already polls every PR once a minute, so the hook belongs there, not in brain.py.'),
      ev('14:03', 'assistant', 'tool_use', 'Bash\ncommand: rg -n "FIX_ROUNDS|def fix" bus/'),
      ev('14:03', 'tool', 'tool_result', 'bus/factory.py:88  FIX_ROUNDS = int(env("LOOM_FIX_ROUNDS", 3))\nbus/brain.py:412  def fix(pr, ticket, findings, lane=None):'),
      ev('14:05', 'thinking', 'text', 'brain.fix already writes a fix brief. Reuse it, so the station only decides when to call it.'),
      ev('14:11', 'assistant', 'tool_use', 'Bash\ncommand: python -B -m unittest tests.test_station_handback'),
      ev('14:11', 'tool', 'tool_result', 'FAIL: test_unresolved_thread_hands_back\n  expected 1 fix brief, got 0'),
      ev('14:11', 'assistant', 'text', 'The new test fails without the fix, as check 1 asks.'),
      ev('14:24', 'assistant', 'tool_use', 'Edit\nfile_path: bus/station.py\n+     for pr in self.open_lane_prs():\n+         if rounds(pr) >= FIX_ROUNDS:\n+             label(pr, "loom:failed"); continue\n+         self.hand_back(pr, unresolved(pr))'),
      ev('14:31', 'assistant', 'tool_use', 'Bash\ncommand: python -B -m unittest discover -s tests'),
      ev('14:31', 'tool', 'tool_result', 'Ran 214 tests in 9.8s\n\nOK'),
    ],
    pending: [
      ev('14:39', 'thinking', 'text', 'Threads a person opened must not count as findings. status already marks them "opened by a person", so reuse that check.'),
      ev('14:40', 'assistant', 'tool_use', 'Bash\ncommand: rg -n "opened by a person" bus/'),
      ev('14:40', 'tool', 'tool_result', 'bus/brain.py:1880  if not is_bot(thread.author): note = "opened by a person"'),
      ev('14:42', 'assistant', 'tool_use', 'Edit\nfile_path: bus/station.py\n+ def unresolved(pr):\n+     return [t for t in threads(pr) if not t.resolved and is_bot(t.author)]'),
      ev('14:44', 'assistant', 'text', 'Person-opened threads are skipped. Running the suite before the runner pushes.'),
    ],
  }],
  grug1146: (() => {
    const runs = [];
    for (let i = 1; i <= 14; i++) {
      const rung = i <= 11 ? 0 : 1;
      const slot = rung === 0 ? (i % 3 === 0 ? 'zen' : 'opencode-go') : 'claude';
      const model = rung === 0 ? (slot === 'zen' ? 'space-bunny-free' : 'kimi-k2.7-code') : 'claude-sonnet-5-5';
      runs.push({
        ticket: 'quadseven/grug#1146', slot, model, outcome: 'failed', started_at: null, finished_at: null,
        events: [
          ev('10:00', 'thinking', 'text', 'Match lines that start with Refs and look the issue up in the same repo.'),
          ev('10:04', 'assistant', 'tool_use', 'Edit\nfile_path: grug/review/context.py\n+  REFS = re.compile(r"^Refs (\\S+)#(\\d+)", re.M)'),
          ev('10:09', 'tool', 'tool_result', rung === 0 ? '3 passed' : 'FAIL: test_refs_crossrepo_midline'),
          ev('10:20', 'runner', 'log', rung === 0 ? 'grug: high finding still open after fix round 3. outcome failed.' : 'validation contract check failed. no PR opened.'),
        ],
      });
    }
    runs.push({
      ticket: 'quadseven/grug#1146', slot: 'codex', model: 'gpt-6-sol', outcome: 'done', started_at: '2026-10-14T15:05:00Z', finished_at: '2026-10-14T15:22:00Z',
      events: [
        ev('15:05', 'user', 'text', 'Rung 2. Attempts 1 to 14 matched Refs only at the start of a line. Do not repeat that. Check bodies where Refs follows a sentence.\n\nWhy: grug cannot read the ticket a lane PR links, so its reviews miss the acceptance criteria.\nWhat: resolve Refs owner/name#N anywhere in the PR body, across repos.\nSize: S'),
        ev('15:06', 'thinking', 'text', 'Lane bodies wrap. "Fixes the parser. Refs quadseven/loom#212" puts Refs mid-line, so the anchored regex never sees it. Match on a word boundary, then resolve owner/name against the API.'),
        ev('15:08', 'assistant', 'tool_use', 'Bash\ncommand: pytest tests/test_refs.py -q'),
        ev('15:08', 'tool', 'tool_result', 'F.\nFAILED test_refs_crossrepo_midline'),
        ev('15:12', 'assistant', 'tool_use', 'Edit\nfile_path: grug/review/context.py\n-  REFS = re.compile(r"^Refs (\\S+)#(\\d+)", re.M)\n+  REFS = re.compile(r"\\bRefs\\s+([\\w.-]+/[\\w.-]+)#(\\d+)")\n+  body = strip_fenced(body)'),
        ev('15:14', 'assistant', 'tool_use', 'Bash\ncommand: pytest -q'),
        ev('15:14', 'tool', 'tool_result', '312 passed in 14.2s'),
        ev('15:15', 'assistant', 'text', 'Pushed. Refs now resolve anywhere in the body, across repos. Replaces #1180.'),
        ev('15:20', 'thinking', 'text', 'grug asks about fenced code. A code sample would pull in an unrelated ticket. strip_fenced runs after the match; move it first and add a test.'),
        ev('15:21', 'assistant', 'tool_use', 'Bash\ncommand: pytest tests/test_refs.py -q'),
        ev('15:21', 'tool', 'tool_result', '4 passed'),
        ev('15:22', 'runner', 'log', 'pushed 9f2c1e4. validation contract passed. outcome done.'),
      ],
    });
    return runs;
  })(),
};

export const lanes = [
  { slot: 'codex', model: 'gpt-6-sol', ticket: 'quadseven/grug#1146', state: 'busy', note: 'merged, idle soon', quota: 64 },
  { slot: 'codex-2', model: 'gpt-6-luna', ticket: 'quadseven/loom#228', state: 'busy', note: 'pr ready', quota: 64 },
  { slot: 'claude', model: 'claude-sonnet-5-5', ticket: null, state: 'free', note: 'subscription', quota: 71 },
  { slot: 'anthropic', model: 'claude-opus-5-5', ticket: 'quadseven/assay#55', state: 'busy', note: '12m', quota: 22 },
  { slot: 'anthropic-2', model: 'claude-opus-5-5', ticket: 'quadseven/loom#94', state: 'busy', note: '41m', quota: 22 },
  { slot: 'anthropic-3', model: 'claude-opus-5-5', ticket: null, state: 'held', note: 'out of credit until 18:00', quota: 0 },
  { slot: 'anthropic-4', model: 'claude-opus-5-5', ticket: 'quadseven/loom#233', state: 'busy', note: '3m', quota: 22 },
  { slot: 'opencode-go', model: 'kimi-k2.7-code', ticket: 'quadseven/assay#41', state: 'busy', note: 'fix round 2', quota: 80 },
  { slot: 'opencode-go-4', model: 'muse-spark-1.3', ticket: null, state: 'free', note: 'public repos only', quota: 100 },
  { slot: 'spark', model: 'local vLLM coder', ticket: null, state: 'held', note: 'box hot, stepping aside', quota: 100 },
  { slot: 'zen', model: 'space-bunny-free', ticket: null, state: 'free', note: 'public repos only', quota: 100 },
  { slot: 'agy', model: 'gemini-3.8-flash-high', ticket: null, state: 'free', note: 'public repos only', quota: 90 },
  { slot: 'zai', model: 'glm-4.7-flash', ticket: null, state: 'free', note: 'public repos only', quota: 100 },
  { slot: 'workersai', model: 'workers-ai', ticket: null, state: 'free', note: '', quota: 100 },
];

// From the current /models page.
export const models = [
  { tier: 'Anthropic credits', share: 32, rows: [['claude-opus-5-5', 'anthropic', 36, 31, 0], ['claude-sonnet-5-5', 'anthropic', 8, 8, 0]] },
  { tier: 'Claude subscription', share: 26, rows: [['claude-sonnet-5-5', 'claude', 31, 28, 2], ['claude-opus-5-5', 'claude', 4, 2, 1]] },
  { tier: 'ChatGPT / Codex', share: 30, rows: [['gpt-6-luna', 'codex', 24, 23, 0], ['gpt-6-sol', 'codex', 17, 17, 0]] },
  { tier: 'OpenCode Go, free tiers, Gemini, Spark', share: 12, rows: [['kimi-k2.7-code', 'opencode-go', 6, 6, 0], ['space-bunny-free', 'zen', 3, 3, 0], ['gemini-3.8-flash-high', 'agy', 2, 2, 0], ['deepseek-v4.1-flash', 'opencode-go', 1, 1, 0], ['glm-4.7-flash', 'zai', 1, 1, 0], ['laguna-s-2.1:free', 'openrouter', 1, 1, 0]] },
];

export const brain = {
  holder: 'muse', started: '14:30', step: 3,
  steps: ['lock', 'status', 'merge', 'verify', 'failed', 'asks', 'brief', 'triage', 'journal', 'unlock'],
  next: { line: 'Fix the assay p95 regression before briefing anything new.', why: 'p95 went 412 to 588ms after assay#38 shipped. It takes the regression slot.' },
  thoughts: [
    { ts: '14:31', tag: 'regression', text: 'assay p95 got worse after assay#38 shipped, 412 to 588ms. It goes first, through the regression slot.' },
    { ts: '14:31', tag: 'operator', text: 'Discord note: "why is grug#1146 still open?" Answered on grug#1143: it is on rung 2 and reviewing clean.' },
    { ts: '14:33', tag: 'merged', text: 'Merged loom#212 and assay#52. Both READY on the current head SHA.' },
    { ts: '14:36', tag: 'verifying', text: 'loom#84 shipped. Waiting to read the 14:30 report on the phone before ticking the live check.' },
    { ts: '14:36', tag: 'deferred', text: 'Triage of example-site goes to the lane machine tick. Out of time budget.' },
  ],
  ranking: [
    { text: 'grug misses cross-repo Refs, so reviews lack context', score: 84, why: '9 PRs x 1.4h lost' },
    { text: "Station waits on spark while its box is hot", score: 61, why: '5 tickets x 2.0h' },
    { text: 'Briefs keep missing the failing-test check', score: 40, why: 'fixed by loom#231, watching' },
  ],
  markers: [
    'done quadseven/loom#212 --stage merge --holder muse',
    'done quadseven/assay#52 --stage merge --holder muse',
    'defer quadseven/example-site#40 --stage triage --holder muse',
  ],
  ticks: [
    { at: '14:30', holder: 'muse', outcome: 'running', line: 'Verifying shipped tickets.' },
    { at: '14:00', holder: 'mac', outcome: 'done', line: 'Shipped loom#84 to the station. Rolled grug.ts.' },
    { at: '13:30', holder: 'muse', outcome: 'done', line: 'Merged 1, briefed 3, escalated grug#1146 to rung 2.' },
    { at: '13:00', holder: 'muse', outcome: 'stopped', line: 'Lock held by mac. Skipped.' },
  ],
};

export const findings = [
  { rule: 'No test fails without the fix', n: 14, trend: 'down' },
  { rule: 'Closing keyword beside an issue number', n: 9, trend: 'flat' },
  { rule: 'Em dash in GitHub text', n: 6, trend: 'down' },
  { rule: 'Bot thread unanswered before merge', n: 4, trend: 'up' },
];
export const laneFail = [['opencode-go', 31], ['zen', 25], ['claude', 9], ['anthropic', 6], ['codex', 4]];

export const improvements = [
  { friction: 'Lanes kept missing the failing-test check', source: 'journal 10-11 14:00', ticket: 'quadseven/loom#231', stage: 3, moved: 'first-pass clean 41% to 58%' },
  { friction: 'grug flagged Refs it could not read', source: 'journal 10-13 22:00', ticket: 'quadseven/grug#1146', stage: 2, moved: null },
  { friction: 'Reports waited for a brain tick', source: 'journal 10-12 10:00', ticket: 'quadseven/loom#84', stage: 2, moved: null },
  { friction: 'The brain sent findings back by hand, 7 times', source: 'journal 10-14 12:00', ticket: 'quadseven/loom#94', stage: 1, moved: null },
  { friction: 'A 429 failed a whole run instead of retrying', source: 'journal 10-14 08:00', ticket: 'quadseven/loom#228', stage: 1, moved: null },
  { friction: "Spark's tickets waited on a hot box", source: 'journal 10-14 14:00', ticket: 'quadseven/loom#234', stage: 1, moved: null },
];

export const asks = [
  { id: 'quadseven/loom#219', title: 'Anthropic credit runs out at 16:00', ask: 'Top up Anthropic credit, or let 3 hard tickets move to codex.', age: '2h', actions: ['Topped up', 'Move to codex'] },
  { id: 'quadseven/assay#58', title: 'Workers AI token missing on the lane machine', ask: 'Add the Workers AI token to the lane machine keychain.', age: '1d', actions: ['Done'] },
];
export const human = 'loom:human: 12 in quadseven/example-app, 3 in quadseven/assay';
