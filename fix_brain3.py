with open('bus/brain.py', 'r') as f:
    content = f.read()

# Replace all lazy imports of factory with ladders
replacements = [
    # Line 626-627: from . import factory -> from . import ladders
    ('from . import factory  # factory imports this module\n    every = set(range(1, len(factory.LADDER) + 1))',
     'from . import ladders  # ladders does not import this module\n    every = set(range(1, len(ladders.LADDER) + 1))'),
    
    # Line 888-890: from . import factory -> from . import ladders
    ('from . import factory  # factory imports this module\n    bot = env.get("LOOM_REVIEW_BOT", "").strip() or factory.DEFAULT_REVIEW_BOT',
     'from . import ladders  # ladders does not import this module\n    bot = env.get("LOOM_REVIEW_BOT", "").strip() or ladders.DEFAULT_REVIEW_BOT'),
    
    # Line 2651-2656: from . import factory, labels -> from . import ladders, labels
    ('from . import factory, labels  # factory imports this module\n             or factory.DEFAULT_REVIEW_CHECK)',
     'from . import ladders, labels  # ladders does not import this module\n             or ladders.DEFAULT_REVIEW_CHECK)'),
    
    # Line 3051-3053: from . import factory -> from . import ladders
    ('            from . import factory\n                   or factory.DEFAULT_REVIEW_BOT)',
     '            from . import ladders\n                   or ladders.DEFAULT_REVIEW_BOT)'),
    
    # Line 3180-3182: from . import factory -> from . import ladders
    ('    from . import factory\n        or factory.DEFAULT_REVIEW_BOT',
     '    from . import ladders\n        or ladders.DEFAULT_REVIEW_BOT'),
    
    # Line 3329-3331: from . import factory -> from . import ladders
    ('    from . import factory  # factory imports this module\n    return raw.strip() or factory.DEFAULT_REVIEW_CHECK',
     '    from . import ladders  # ladders does not import this module\n    return raw.strip() or ladders.DEFAULT_REVIEW_CHECK'),
    
    # Line 4068-4069: from . import factory -> from . import ladders
    ('    from . import factory  # factory imports this module\n    return not notes.strip() or notes.strip() == factory.FIX_NOTES.strip()',
     '    from . import ladders  # ladders does not import this module\n    return not notes.strip() or notes.strip() == ladders.FIX_NOTES.strip()'),
    
    # Line 4566: comment reference to factory.CLIMB
    ('        self.rung = rung  # the next rung (0-based in factory.CLIMB), or None past the ladder',
     '        self.rung = rung  # the next rung (0-based in ladders.CLIMB), or None past the ladder'),
    
    # Line 4568: comment reference to factory.tried_rungs
    ('        # trusted rung markers (bus/factory.py tried_rungs).',
     '        # trusted rung markers (bus/ladders.py tried_rungs).'),
    
    # Line 4583-4584: from .factory import CLIMB -> from .ladders import CLIMB
    ('        from .factory import CLIMB  # factory imports this module\n        return "next rung %d %s" % (self.rung + 1, CLIMB[self.rung][1])',
     '        from .ladders import CLIMB  # ladders does not import this module\n        return "next rung %d %s" % (self.rung + 1, CLIMB[self.rung][1])'),
    
    # Line 4609: comment reference to factory.EQUIVALENTS
    ('    force (an escalation or an equivalent move, bus/factory.py EQUIVALENTS,',
     '    force (an escalation or an equivalent move, bus/ladders.py EQUIVALENTS,'),
    
    # Line 4612: comment reference to factory.claim_models
    ('    configured model (lane_model), as bus/factory.py claim_models reads it."""',
     '    configured model (lane_model), as bus/ladders.py claim_models reads it."""'),
    
    # Line 4613: from .factory import EQUIVALENT_RE -> from .ladders import EQUIVALENT_RE
    ('    from .factory import EQUIVALENT_RE  # factory imports this module\n    tried: List[str] = []',
     '    from .ladders import EQUIVALENT_RE  # ladders does not import this module\n    tried: List[str] = []'),
    
    # Line 4690-4692: from . import factory -> from . import ladders
    ('    from . import factory  # factory imports this module\n    return factory.tried_rungs([c for c in comments if isinstance(c, dict) and str(',
     '    from . import ladders  # ladders does not import this module\n    return ladders.tried_rungs([c for c in comments if isinstance(c, dict) and str('),
    
    # Line 4720-4722: comment and import
    ('def _done_rungs(tried: List[str], rungs: Optional[set] = None) -> set:\n    """The rungs of factory.CLIMB tried: from trusted rung markers, and each\n    whose model a claim ran."""\n    from .factory import CLIMB  # factory imports this module\n    return set(rungs or ()) | {n for n, (_, model) in enumerate(CLIMB, 1) if model in tried}',
     'def _done_rungs(tried: List[str], rungs: Optional[set] = None) -> set:\n    """The rungs of ladders.CLIMB tried: from trusted rung markers, and each\n    whose model a claim ran."""\n    from .ladders import CLIMB  # ladders does not import this module\n    return set(rungs or ()) | {n for n, (_, model) in enumerate(CLIMB, 1) if model in tried}'),
    
    # Line 4727-4729: comment and import
    ('def _lowest_untried(tried: List[str], rungs: Optional[set] = None) -> Optional[int]:\n    """The next rung the ticket may climb to (bus/factory.py climb_rungs),\n    0-based, or None when none is left.""",\n    from .factory import climb_rungs  # factory imports this module\n    left = climb_rungs(_done_rungs(tried, rungs), max(rungs or {0}))\n    return left[0] - 1 if left else None',
     'def _lowest_untried(tried: List[str], rungs: Optional[set] = None) -> Optional[int]:\n    """The next rung the ticket may climb to (bus/ladders.py climb_rungs),\n    0-based, or None when none is left.""",\n    from .ladders import climb_rungs  # ladders does not import this module\n    left = climb_rungs(_done_rungs(tried, rungs), max(rungs or {0}))\n    return left[0] - 1 if left else None'),
    
    # Line 4844-4854: from . import factory and references
    ('    from . import factory  # factory imports this module\n    found_pr = _open_pr(gh, repo, number)\n    # Its pull request\'s runner footer names the model its newest run used,\n    # read only from a LOOM_BRIEF_AUTHORS login\'s pull request whose head and\n    # base are in this repo (anyone can open one that matches the ticket on a\n    # public repo), and only when it names a known model (factory.footer_run).\n    footer = None\n    if found_pr is not None and _same_repo(repo, found_pr, {}) and str(\n            (found_pr.get("user") or {}).get("login") or "").lower() in {\n            a.lower() for a in trusted_authors()}:\n        footer = factory.footer_run(found_pr.get("body"))',
     '    from . import ladders  # ladders does not import this module\n    found_pr = _open_pr(gh, repo, number)\n    # Its pull request\'s runner footer names the model its newest run used,\n    # read only from a LOOM_BRIEF_AUTHORS login\'s pull request whose head and\n    # base are in this repo (anyone can open one that matches the ticket on a\n    # public repo), and only when it names a known model (ladders.footer_run).\n    footer = None\n    if found_pr is not None and _same_repo(repo, found_pr, {}) and str(\n            (found_pr.get("user") or {}).get("login") or "").lower() in {\n            a.lower() for a in trusted_authors()}:\n        footer = ladders.footer_run(found_pr.get("body"))'),
    
    # Line 4870-4876: factory.CLIMB, factory.PUBLIC_ONLY
    ('        lanes = factory.CLIMB[n - 1][0]\n        if any(lane in factory.PUBLIC_ONLY for lane in lanes):\n            if not private:\n                data = gh.rest("GET", "/repos/%s" % repo).data\n                private.append(not isinstance(data, dict) or data.get("private") is not False)\n            if private[0]:\n                lanes = tuple(lane for lane in lanes if lane not in factory.PUBLIC_ONLY)',
     '        lanes = ladders.CLIMB[n - 1][0]\n        if any(lane in ladders.PUBLIC_ONLY for lane in lanes):\n            if not private:\n                data = gh.rest("GET", "/repos/%s" % repo).data\n                private.append(not isinstance(data, dict) or data.get("private") is not False)\n            if private[0]:\n                lanes = tuple(lane for lane in lanes if lane not in ladders.PUBLIC_ONLY)'),
    
    # Line 4879: factory.climb_rungs
    ('    left = [n for n in factory.climb_rungs(_done_rungs(history.tried, history.rungs),\n                                           max(history.rungs or {0})) if rung_lanes(n)]',
     '    left = [n for n in ladders.climb_rungs(_done_rungs(history.tried, history.rungs),\n                                           max(history.rungs or {0})) if rung_lanes(n)]'),
    
    # Line 4899: factory.CLIMB
    ('    model = factory.CLIMB[rung][1]',
     '    model = ladders.CLIMB[rung][1]'),
    
    # Line 4902: len(factory.CLIMB)
    ('                         "Escalated to rung %d of %d: %s on lane %s." % (rung + 1, len(factory.CLIMB), model, lane),',
     '                         "Escalated to rung %d of %d: %s on lane %s." % (rung + 1, len(ladders.CLIMB), model, lane),'),
]

for old, new in replacements:
    if old in content:
        content = content.replace(old, new)
        print("Replaced one")
    else:
        print("NOT FOUND:")
        print(repr(old[:100]))

with open('bus/brain.py', 'w') as f:
    f.write(content)

print("Done")