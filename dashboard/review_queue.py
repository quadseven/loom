"""Held pull requests, their evidence, and the existing operator answer controls."""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urlsplit

from bus import answer, brain, proof, criteria_gate
from . import needs_you


def held(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep every held ask, including asks whose PR detail is unavailable."""
    return [entry for entry in entries if answer.held_pr(needs_you.full_ask(entry)) is not None]


def criteria(body: str) -> str:
    """Checkboxes are claims, not deployment proof. Never upgrade their meaning."""
    boxes = proof.criteria(body)
    if not boxes:
        return '<p class="muted">Acceptance criteria unavailable.</p>'
    return '<ul class="review-criteria">' + ''.join(
        '<li><span class="machine-fact">%s / %s</span> %s</li>' % (
            'Claimed' if mark else 'Pending', html.escape(criteria_gate.kind(text)), html.escape(text))
        for mark, text in boxes) + '</ul>'


def _findings(pr: Dict[str, Any]) -> str:
    threads = pr.get('threads') or []
    rows = ''.join('<li><span class="machine-fact">%s</span> %s</li>' % (
        'Resolved' if row.get('resolved') else 'Open', html.escape(str(row.get('text') or '')))
        for row in threads)
    reviews = ''.join('<details><summary>Review summary</summary><pre>%s</pre></details>' %
                      html.escape(str(review.get('body') or '')) for review in pr.get('reviews') or [])
    return ('<h3>Elder findings</h3>' + ('<ul>' + rows + '</ul>' if rows else
            '<p class="muted">No inline findings in this read.</p>') + reviews)


def card(entry: Dict[str, Any], detail: Dict[str, Any], mode: str) -> str:
    esc = html.escape
    repo, ticket = str(entry.get('repo') or ''), int(entry['number'])
    pr_number = answer.held_pr(needs_you.full_ask(entry))
    title = str(entry.get('title') or 'Held pull request')
    link = _title_link(title, detail.get('pr'))
    out = ['<article class="review-item"><h2>%s</h2>' % link,
           '<p class="machine-fact">%s - pull request #%s - ticket #%d</p>' %
           (esc(repo), pr_number, ticket), '<h3>Why it waits</h3><p>%s</p>' %
           esc(str((detail.get('review_ask') or {}).get('text') or needs_you.full_ask(entry)))]
    pr, errors = detail.get('pr'), detail.get('errors') or {}
    valid = isinstance(pr, dict) and pr.get('number') == pr_number
    out.append(_summary(pr if valid else None))
    for name in errors:
        out.append('<p class="review-error">%s unavailable. Try again in five minutes.</p>' % esc(name))
    out.append('<p class="machine-fact">Ship proof: %s</p>' % esc(str(detail.get('proof_status') or 'Not read')))
    out.extend(['<h3>Acceptance and ship proof</h3>', criteria(str(detail.get('body') or entry.get('body') or '')),
                '<p class="muted">Checked boxes are author claims. The merge bar independently checks proof; '
                'a merge does not establish deployment or live acceptance.</p>'])
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
    return needs_you.answer_form(dict(entry, body_ask=ask['text']), mode).replace(
        'data-answer ', 'data-answer data-review-pr="%d" data-review-head="%s" data-review-ask="%s" ' % (
            pr['number'], html.escape(str(pr['head']), quote=True), html.escape(ask['key'], quote=True)))


def _review_facts(number: int, pr: Dict[str, Any], tier: str, head: str,
                  threads: List[Dict[str, Any]], reviews: List[Dict[str, Any]],
                  checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {'number': number, 'title': pr.get('title'), 'html_url': pr.get('html_url'),
            'head': head, 'tier': tier,
            'state': 'merged' if pr.get('merged_at') else pr.get('state'),
            'threads': [{'text': str(node['comments']['nodes'][0].get('body') or ''),
                         'resolved': bool(node.get('isResolved'))} for node in threads],
            'reviews': reviews,
            'checks': [{'name': c.get('name'), 'state': c.get('conclusion') or c.get('status')}
                       for c in checks]}


def render(entries: Iterable[Dict[str, Any]], details: Dict[str, Any], mode: str = 'off') -> str:
    rows = held(entries)
    if not rows:
        return '<p class="review-empty">No pull requests are waiting for an operator in this snapshot.</p>'
    return '<div class="review-queue">' + ''.join(card(
        row, details.get('%s#%d' % (row['repo'], row['number']), {}), mode) for row in rows) + '</div>'


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
        pr_number = answer.held_pr(needs_you.full_ask(entry))
        key = (repo, number, pr_number, needs_you.full_ask(entry))
        with self.lock:
            now = time.monotonic()
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 300}
            if key not in self.cache:
                value = self._read(repo, number, pr_number)
                if len(self.cache) >= 64:
                    del self.cache[min(self.cache, key=lambda k: self.cache[k][0])]
                self.cache[key] = (now, value)
            return self.cache[key][1]

    def _read(self, repo: str, ticket: int, number: int) -> Dict[str, Any]:
        from bus.github import GitHubError
        from bus import merge_tier
        result: Dict[str, Any] = {'errors': {}}
        try:
            base = '/repos/%s' % repo
            issue = self.gh.rest('GET', '%s/issues/%d' % (base, ticket)).data
            pr = self.gh.rest('GET', '%s/pulls/%d' % (base, number)).data
            if not isinstance(issue, dict) or not isinstance(pr, dict):
                raise ValueError('issue or pull request unavailable')
            files = self.gh.paginate('%s/pulls/%d/files' % (base, number))
            head = str((pr.get('head') or {}).get('sha') or '')
            if not re.fullmatch('[0-9a-f]{40}', head):
                raise ValueError('pull request head unavailable')
            tier, _ = merge_tier.classify(repo, files, str(issue.get('body') or ''),
                                         any(x.get('name') == 'class:sensitive' for x in pr.get('labels') or []))
            checks = self._checks(base, head)
            reviews = self.gh.paginate('%s/pulls/%d/reviews' % (base, number))
            comments = self.gh.paginate('%s/issues/%d/comments' % (base, ticket))
            result['review_ask'] = self._ask_facts(comments, number)
            result['proof_status'] = self._proof_status(comments, str(issue.get('body') or ''))
            raw_threads = self._threads(repo, number)
            result.update(body=str(issue.get('body') or ''), pr=_review_facts(
                number, pr, tier, head, raw_threads, self._elder_reviews(reviews), checks))
        except (GitHubError, OSError, ValueError, KeyError, TypeError):
            result['errors']['Review facts'] = 'unavailable'
        return result

    @staticmethod
    def _ask_facts(comments: List[Dict[str, Any]], number: int) -> Dict[str, str]:
        ask = answer.latest_ask(comments, brain.trusted_authors())
        if ask is None or answer.held_pr(str(ask.get('body') or '')) != number:
            raise ValueError('current review ask unavailable')
        return {'key': answer.review_ask_key(ask),
                'text': str(ask['body']).replace(answer.HUMAN_MARKER, '', 1).strip()}

    @staticmethod
    def _proof_status(comments: List[Dict[str, Any]], body: str) -> str:
        from bus.brain import trusted_authors
        trusted = {name.lower() for name in trusted_authors()}
        for comment in reversed(comments):
            if str((comment.get('user') or {}).get('login') or '').lower() not in trusted:
                continue
            record = proof.read(str(comment.get('body') or ''))
            if record is not None:
                try:
                    proof.check(record, body)
                except ValueError:
                    return 'Recorded proof does not match the current acceptance contract'
                return 'Matching proof recorded; inspect its sources on the ticket'
        return 'No matching trusted proof record'

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
