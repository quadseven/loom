with open('bus/brain.py', 'r') as f:
    content = f.read()

# Fix remaining factory references with exact formatting
replacements = [
    # Line 888-890
    ('    from . import factory  # factory imports this module\n    env = os.environ if env is None else env\n    bot = env.get("LOOM_REVIEW_BOT", "").strip() or factory.DEFAULT_REVIEW_BOT',
     '    from . import ladders  # ladders does not import this module\n    env = os.environ if env is None else env\n    bot = env.get("LOOM_REVIEW_BOT", "").strip() or ladders.DEFAULT_REVIEW_BOT'),
    
    # Line 2651-2656
    ('    from . import factory, labels  # factory imports this module\n    env = os.environ if env is None else env\n    if operator is not None and not tickets._OPERATOR_RE.match("Operator: " + operator):\n        raise ValueError("--operator %r is not a GitHub login" % operator)\n    check = (review_check or env.get("LOOM_REVIEW_CHECK", "").strip()\n             or factory.DEFAULT_REVIEW_CHECK)',
     '    from . import ladders, labels  # ladders does not import this module\n    env = os.environ if env is None else env\n    if operator is not None and not tickets._OPERATOR_RE.match("Operator: " + operator):\n        raise ValueError("--operator %r is not a GitHub login" % operator)\n    check = (review_check or env.get("LOOM_REVIEW_CHECK", "").strip()\n             or ladders.DEFAULT_REVIEW_CHECK)'),
    
    # Line 3051-3053
    ('        if change_class.class_of(pr) == "sensitive":\n            from . import factory\n            bot = (os.environ.get("LOOM_REVIEW_BOT", "").strip()\n                   or factory.DEFAULT_REVIEW_BOT)\n            _validate_sensitive_merge(gh, repo, details, bot, review_check_name(env))',
     '        if change_class.class_of(pr) == "sensitive":\n            from . import ladders\n            bot = (os.environ.get("LOOM_REVIEW_BOT", "").strip()\n                   or ladders.DEFAULT_REVIEW_BOT)\n            _validate_sensitive_merge(gh, repo, details, bot, review_check_name(env))'),
    
    # Line 3180-3182
    ('    from . import factory\n    bot = (env if env is not None else os.environ).get("LOOM_REVIEW_BOT", "").strip() \\\n        or factory.DEFAULT_REVIEW_BOT\n    blocking = merge_tier.blocking_findings(',
     '    from . import ladders\n    bot = (env if env is not None else os.environ).get("LOOM_REVIEW_BOT", "").strip() \\\n        or ladders.DEFAULT_REVIEW_BOT\n    blocking = merge_tier.blocking_findings('),
    
    # Line 3329-3331
    ('def review_check_name(env: Optional[Dict[str, str]] = None) -> str:\n    """The check run that must have run on a head before it lands: the\n    operator\'s named review check, `LOOM_REVIEW_CHECK` (factory default)."""\n    from . import factory  # factory imports this module\n    raw = (os.environ if env is None else env).get("LOOM_REVIEW_CHECK", "")\n    return raw.strip() or factory.DEFAULT_REVIEW_CHECK',
     'def review_check_name(env: Optional[Dict[str, str]] = None) -> str:\n    """The check run that must have run on a head before it lands: the\n    operator\'s named review check, `LOOM_REVIEW_CHECK` (ladders default)."""\n    from . import ladders  # ladders does not import this module\n    raw = (os.environ if env is None else env).get("LOOM_REVIEW_CHECK", "")\n    return raw.strip() or ladders.DEFAULT_REVIEW_CHECK'),
    
    # Line 4690-4692
    ('def _trusted_rungs(comments: List[Any], authors: Sequence[str]) -> set:\n    """The rungs a ticket ran on, from rung markers by a trusted login only,\n    as in the factory: anyone can comment on a public repo.""",\n    from . import factory  # factory imports this module\n    trusted = {a.lower() for a in authors}\n    return factory.tried_rungs([c for c in comments if isinstance(c, dict) and str(',
     'def _trusted_rungs(comments: List[Any], authors: Sequence[str]) -> set:\n    """The rungs a ticket ran on, from rung markers by a trusted login only,\n    as in the factory: anyone can comment on a public repo.""",\n    from . import ladders  # ladders does not import this module\n    trusted = {a.lower() for a in authors}\n    return ladders.tried_rungs([c for c in comments if isinstance(c, dict) and str('),
]

for old, new in replacements:
    if old in content:
        content = content.replace(old, new)
        print("Replaced one")
    else:
        print("NOT FOUND:")
        print(repr(old[:150]))

with open('bus/brain.py', 'w') as f:
    f.write(content)

print("Done")