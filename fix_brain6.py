with open('bus/brain.py', 'r') as f:
    content = f.read()

# Fix the last factory reference
old = '''def _trusted_rungs(comments: List[Any], authors: Sequence[str]) -> set:
    """The rungs a ticket ran on, from rung markers by a trusted login only,
    as in the factory: anyone can comment on a public repo."""
    from . import factory  # factory imports this module
    trusted = {a.lower() for a in authors}
    return factory.tried_rungs([c for c in comments if isinstance(c, dict) and str(
        (c.get("user") or {}).get("login") or "").lower() in trusted])'''

new = '''def _trusted_rungs(comments: List[Any], authors: Sequence[str]) -> set:
    """The rungs a ticket ran on, from rung markers by a trusted login only,
    as in the factory: anyone can comment on a public repo."""
    from . import ladders  # ladders does not import this module
    trusted = {a.lower() for a in authors}
    return ladders.tried_rungs([c for c in comments if isinstance(c, dict) and str(
        (c.get("user") or {}).get("login") or "").lower() in trusted])'''

if old in content:
    content = content.replace(old, new)
    print("Replaced successfully")
else:
    print("NOT FOUND")

with open('bus/brain.py', 'w') as f:
    f.write(content)

print("Done")