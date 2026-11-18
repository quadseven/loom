"""Held pull requests, their evidence, and the existing operator answer controls."""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urlsplit

from bus import answer, brain, proof, criteria_gate
from . import needs_you


def candidates(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Read waiting lane tickets even when the generic ask preview missed a hold."""
    return [entry for entry in entries if 'loom:pr' in entry.get('labels', ())
            or answer.held_pr(needs_you.full_ask(entry)) is not None]


def criteria(body: str, recorded: Iterable[str] = ()) -> str:
    boxes = proof.criteria(body)
    if not boxes:
        return '<p class="muted">Acceptance criteria unavailable.</p>'
    evidence_needed = {criteria_gate.CODE: 'Awaiting shipped change',
                       criteria_gate.LIVE: 'Awaiting station evidence',
                       criteria_gate.OPERATOR: 'Operator verification required'}
    statuses = {kind: 'Proof recorded' if kind in recorded else needed
                for kind, needed in evidence_needed.items()}
    return '<ul class="review-criteria">' + ''.join(
        '<li>%s <span class="machine-fact">%s / %s</span></li>' % (
            html.escape(text), 'Claimed' if mark else 'Pending', statuses[criteria_gate.kind(text)])
        for mark, text in boxes) + '</ul>'


def _findings(pr: Dict[str, Any]) -> str:
    threads = pr.get('threads') or []
    rows = ''.join('<li><span class="machine-fact">%s</span> %s</li>' % (
        'Resolved' if row.get('resolved') else 'Open', html.escape(str(row.get('text') or '')))
        for row in threads)
    reviews = ''.join('<details><summary>Review summary</summary><pre>%s</pre></details>' %
                      html.escape(str(review.get('body') or '')) for review in pr.get('reviews') or [])
    summary = pr.get('review_summary')
    if summary:
        reviews = '<details open><summary>Head review report</summary><pre>%s</pre></details>' % (
            html.escape(summary)) + reviews
    return ('<h3>Elder findings</h3>' + ('<ul>' + rows + '</ul>' if rows else
            '<p class="muted">No inline findings in this read.</p>') + reviews)


def card(entry: Dict[str, Any], detail: Dict[str, Any], mode: str) -> str:
    esc = html.escape
    repo, ticket = str(entry.get('repo') or ''), int(entry['number'])
    ask = detail.get('review_ask') or {}
    pr_number = ask.get('number') or answer.held_pr(needs_you.full_ask(entry))
    pr, errors = detail.get('pr'), detail.get('errors') or {}
    valid = isinstance(pr, dict) and pr.get('number') == pr_number
    title = str((pr.get('title') if valid else None) or entry.get('title') or 'Held pull request')
    link = _title_link(title, pr if valid else None)
    out = ['<article class="review-item"><h2>%s</h2>' % link,
           '<p class="machine-fact">%s - pull request #%s - ticket #%d</p>' %
           (esc(repo), pr_number, ticket), '<h3>Why it waits</h3><p>%s</p>' %
           esc(str(ask.get('text') or needs_you.full_ask(entry)))]
    out.append(_summary(pr if valid else None))
    for name in errors:
        out.append('<p class="review-error">%s unavailable. Try again in five minutes.</p>' % esc(name))
    out.append('<p class="machine-fact">Ship proof: %s</p>' % esc(str(detail.get('proof_status') or 'Not read')))
    out.extend(['<h3>Verify before ship</h3>', criteria(str(detail.get('body') or entry.get('body') or ''),
                                                      detail.get('proof_kinds', ()))])
    out.append(_actions(entry, pr if valid else None, errors, mode, detail.get('review_ask')))
    out.append('</article>')
    return ''.join(out)


def _title_link(title: str, pr: Dict[str, Any] | None) -> str:
    """Use the API's public link, never an executable URI or a fabricated host."""
    url = str(pr.get('html_url') or '') if isinstance(pr, dict) else ''
    try:
        parsed = urlsplit(url)
    except ValueError:
        return html.escape(title)
    if parsed.scheme != 'https' or not parsed.netloc:
        return html.escape(title)
    return '<a href="%s">%s</a>' % (html.escape(url, quote=True), html.escape(title))


def _summary(pr: Dict[str, Any] | None) -> str:
    if pr is None:
        return '<p class="review-error" role="status">Details unavailable. Try again in five minutes.</p>'
    esc = html.escape
    summary = '<p class="machine-fact">Tier: %s | State: %s | Head: %s</p>' % (
        esc(str(pr.get('tier') or 'Unread')), esc(str(pr.get('state') or 'Unread')),
        esc(str(pr.get('head') or 'Unread')[:12]))
    checks = ', '.join('%s: %s' % (c.get('name'), c.get('state')) for c in pr.get('checks') or [])
    return summary + _findings(pr) + '<h3>Checks</h3><p>%s</p>' % esc(
        checks or 'No check runs returned for this head.')


def _actions(entry: Dict[str, Any], pr: Dict[str, Any] | None,
             errors: Dict[str, Any], mode: str, ask: Dict[str, str] | None = None) -> str:
    if entry.get('pending'):
        return '<p role="status">Answer recorded; waiting for the station.</p>'
    if pr is None or errors or pr.get('state') != 'open' or not pr.get('head') or not ask:
        return ''
    approval = 'Approve for brain review' if pr.get('brain_review') else 'Approve and merge'
    return needs_you.answer_form(dict(entry, body_ask=ask['text']), mode).replace(
        '>Merge</button>', '>%s</button>' % approval).replace(
        'data-answer ', 'data-answer data-review-pr="%d" data-review-head="%s" data-review-ask="%s" ' % (
            pr['number'], html.escape(str(pr['head']), quote=True), html.escape(ask['key'], quote=True)))


def _review_facts(number: int, pr: Dict[str, Any], tier: str, head: str,
                  threads: List[Dict[str, Any]], reviews: List[Dict[str, Any]],
                  checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {'number': number, 'title': pr.get('title'), 'html_url': pr.get('html_url'),
            'head': head, 'tier': tier,
            'brain_review': any(label.get('name') == 'class:sensitive' for label in pr.get('labels') or []),
            'state': 'merged' if pr.get('merged_at') else pr.get('state'),
            'review_summary': '\n\n'.join(str((check.get('output') or {}).get('summary') or '')
                                         for check in checks if check.get('name') == brain.review_check_name()),
            'threads': [{'text': str(node['comments']['nodes'][0].get('body') or ''),
                         'resolved': bool(node.get('isResolved'))} for node in threads],
            'reviews': reviews,
            'checks': [{'name': c.get('name'), 'state': c.get('conclusion') or c.get('status')}
                       for c in checks]}


def render(entries: Iterable[Dict[str, Any]], details: Dict[str, Any], mode: str = 'off') -> str:
    rows = []
    for entry in entries:
        detail = details.get('%s#%d' % (entry['repo'], entry['number']), {})
        ask = detail.get('review_ask')
        if 'review_ask' not in detail:
            ask = answer.held_pr(needs_you.full_ask(entry))
        if ask and (detail.get('pr') or {}).get('state', 'open') == 'open':
            rows.append(card(entry, detail, mode))
    if not rows:
        return '<p class="review-empty">No pull requests are waiting for an operator in this snapshot.</p>'
    return '<div class="review-queue">' + ''.join(rows) + '</div>'


def _review_bot_login() -> str:
    """Canonical REST identity; GraphQL Bot authors normalize to the same login."""
    import os
    configured = os.environ.get('LOOM_REVIEW_BOT', '').strip().lower() or 'grug-tribe'
    return configured if configured.endswith('[bot]') else configured + '[bot]'


class Details:
    """Read review facts once per five minutes; incomplete reads remain visible."""
    def __init__(self, gh: Any) -> None:
        import threading
        self.gh = gh
        self.cache: Dict[tuple, tuple] = {}
        self.lock = threading.Lock()

    def get(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        import time
        repo, number = str(entry['repo']), int(entry['number'])
        key = (repo, number, entry.get('updated_at'))
        with self.lock:
            now = time.monotonic()
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 300}
            if key not in self.cache:
                self.cache[key] = (now, self._read(repo, number))
            return self.cache[key][1]

    def _read(self, repo: str, ticket: int) -> Dict[str, Any]:
        from bus.github import GitHubError
        from bus import merge_tier
        result: Dict[str, Any] = {'errors': {}}
        try:
            base = '/repos/%s' % repo
            comments = self.gh.paginate('%s/issues/%d/comments' % (base, ticket))
            ask = self._ask_facts(comments)
            result['review_ask'] = ask
            if ask is None:
                return result
            number = ask['number']
            pr = self.gh.rest('GET', '%s/pulls/%d' % (base, number)).data
            if not isinstance(pr, dict) or 'state' not in pr:
                raise ValueError('pull request unavailable')
            if pr.get('state') != 'open' or pr.get('merged_at'):
                result['review_ask'] = None
                return result
            issue = self.gh.rest('GET', '%s/issues/%d' % (base, ticket)).data
            if not isinstance(issue, dict):
                raise ValueError('ticket unavailable')
            if issue.get('state') == 'closed':
                result['review_ask'] = None
                return result
            files = self.gh.paginate('%s/pulls/%d/files' % (base, number))
            head = str((pr.get('head') or {}).get('sha') or '')
            if not re.fullmatch('[0-9a-f]{40}', head):
                raise ValueError('pull request head unavailable')
            tier, _ = merge_tier.classify(repo, files, str(issue.get('body') or ''),
                                         any(x.get('name') == 'class:sensitive' for x in pr.get('labels') or []))
            body = str(issue.get('body') or '')
            result['body'] = body
            result['proof_status'], result['proof_kinds'] = self._proof_status(comments, body, head)
            checks = self._checks(base, head)
            reviews = self.gh.paginate('%s/pulls/%d/reviews' % (base, number))
            raw_threads = self._threads(repo, number)
            result['pr'] = _review_facts(number, pr, tier, head, raw_threads,
                                         self._elder_reviews(reviews), checks)
        except (GitHubError, OSError, ValueError, KeyError, TypeError):
            result['errors']['Review facts'] = 'unavailable'
        return result

    @staticmethod
    def _ask_facts(comments: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        ask = answer.latest_ask(comments, brain.trusted_authors())
        text = str(ask['body']).removeprefix(answer.HUMAN_MARKER).strip() if ask else ''
        number = answer.held_pr(text)
        if number is None:
            return None
        return {'number': number, 'key': answer.review_ask_key(ask), 'text': text}

    @staticmethod
    def _proof_status(comments: List[Dict[str, Any]], body: str, head: str) -> tuple[str, List[str]]:
        trusted = {name.lower() for name in brain.trusted_authors()}
        for comment in reversed(comments):
            if str((comment.get('user') or {}).get('login') or '').lower() not in trusted:
                continue
            record = proof.read(str(comment.get('body') or ''))
            if record is not None:
                if record.get('revision') != head:
                    return 'Recorded proof is for a different revision', []
                if record.get('contract') != proof.contract(body):
                    return 'Recorded proof does not match the current acceptance contract', []
                try:
                    proof.check(record, body)
                except ValueError:
                    return 'Recorded proof does not match the current acceptance contract', []
                kinds = [criteria_gate.CODE]
                if record.get('kind') == 'runtime' and record.get(proof.FACTS_KEY):
                    kinds.append(criteria_gate.LIVE)
                return 'Matching proof recorded; inspect its sources on the ticket', kinds
        return 'No matching trusted proof record', []

    def _checks(self, base: str, head: str) -> List[Dict[str, Any]]:
        found = []
        for page in range(1, 101):
            data = self.gh.rest('GET', '%s/commits/%s/check-runs?per_page=100&page=%d' %
                                (base, head, page)).data
            rows = data['check_runs']
            found.extend(rows)
            if len(found) >= data['total_count']:
                return found
            if not rows:
                raise ValueError('check pagination incomplete')
        raise ValueError('check pagination incomplete')

    @staticmethod
    def _elder_reviews(reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        bot = _review_bot_login()
        return [r for r in reviews if brain.comment_login(r).lower() == bot
                and str(r.get('body') or '').strip()]

    def _threads(self, repo: str, number: int) -> List[Dict[str, Any]]:
        owner, name = repo.split('/', 1)
        query = ('query($owner:String!,$name:String!,$number:Int!,$cursor:String) {'
                 ' repository(owner:$owner,name:$name) { pullRequest(number:$number) {'
                 ' reviewThreads(first:100,after:$cursor) { pageInfo { hasNextPage endCursor }'
                 ' nodes { isResolved comments(first:1) { nodes { author { login __typename } body createdAt } } } } } } }')
        bot = _review_bot_login()
        cursor, seen, result = None, set(), []
        while True:
            data = self.gh.graphql(query, {'owner': owner, 'name': name, 'number': number, 'cursor': cursor})
            page = data['repository']['pullRequest']['reviewThreads']
            for node in page['nodes']:
                comments = node.get('comments', {}).get('nodes') or []
                if comments and brain.comment_login(comments[0]).lower() == bot:
                    result.append(node)
            info = page['pageInfo']
            if not info['hasNextPage']:
                return result
            cursor = info.get('endCursor')
            if not cursor or cursor in seen or len(seen) >= 100:
                raise ValueError('review thread pagination incomplete')
            seen.add(cursor)
