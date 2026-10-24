with open('bus/brain.py', 'r') as f:
    content = f.read()

# Fix remaining factory references
replacements = [
    # Line 888-890
    ('    from . import factory  # factory imports this module\n    bot = env.get("LOOM_REVIEW_BOT", "").strip() or factory.DEFAULT_REVIEW_BOT',
     '    from . import ladders  # ladders does not import this module\n    bot = env.get("LOOM_REVIEW_BOT", "").strip() or ladders.DEFAULT_REVIEW_BOT'),
    
    # Line 2083: comment reference
    ('    places it as factory.DEFAULT_DIFFICULTY, which may be the wrong tier.',
     '    places it as ladders.DEFAULT_DIFFICULTY, which may be the wrong tier.'),
    
    # Line 2651-2656
    ('    from . import factory, labels  # factory imports this module\n             or factory.DEFAULT_REVIEW_CHECK)',
     '    from . import ladders, labels  # ladders does not import this module\n             or ladders.DEFAULT_REVIEW_CHECK)'),
    
    # Line 2817: comment reference
    ('    (factory.DEFAULT_DIFFICULTY); before this a brain could only set its tier',
     '    (ladders.DEFAULT_DIFFICULTY); before this a brain could only set its tier'),
    
    # Line 3051-3053
    ('            from . import factory\n                   or factory.DEFAULT_REVIEW_BOT)',
     '            from . import ladders\n                   or ladders.DEFAULT_REVIEW_BOT)'),
    
    # Line 3180-3182
    ('    from . import factory\n        or factory.DEFAULT_REVIEW_BOT',
     '    from . import ladders\n        or ladders.DEFAULT_REVIEW_BOT'),
    
    # Line 3329-3331
    ('    from . import factory  # factory imports this module\n    return raw.strip() or factory.DEFAULT_REVIEW_CHECK',
     '    from . import ladders  # ladders does not import this module\n    return raw.strip() or ladders.DEFAULT_REVIEW_CHECK'),
    
    # Line 4541: comment reference
    ('# builds on merged, is stuck in the same way (bus/factory.py HOLD_STUCK',
     '# builds on merged, is stuck in the same way (bus/ladders.py HOLD_STUCK'),
    
    # Line 4690-4692
    ('    from . import factory  # factory imports this module\n    return factory.tried_rungs([c for c in comments if isinstance(c, dict) and str(',
     '    from . import ladders  # ladders does not import this module\n    return ladders.tried_rungs([c for c in comments if isinstance(c, dict) and str('),
    
    # Line 4727: comment reference
    ('    """The next rung the ticket may climb to (bus/factory.py climb_rungs),',
     '    """The next rung the ticket may climb to (bus/ladders.py climb_rungs),'),
    
    # Line 4729
    ('    from .factory import climb_rungs  # factory imports this module\n    left = climb_rungs(_done_rungs(tried, rungs), max(rungs or {0}))',
     '    from .ladders import climb_rungs  # ladders does not import this module\n    left = climb_rungs(_done_rungs(tried, rungs), max(rungs or {0}))'),
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