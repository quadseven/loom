# The ask block

An ask is an issue a person must act on (`needs:human`, or an old name of it). The daily Discord digest forwards an ask to the operator only after a brain has vetted it. Vetting means the brain read the live facts, reduced the ask to one question, and wrote its answer. The proof is the ask block.

## Format

The ask block is a comment on the ask ticket. It begins with this marker on its first line, then three field lines, in any order:

```
<!-- loom:ask v1 -->
Ask: <one question for the person>
Recommend: <the answer the brain advises>
Why: <one line of reason>
```

- All three fields are required and hold one line each. A block missing a field is not a block.
- Only a login in `LOOM_BRIEF_AUTHORS` (the brains' own Apps) can vet. A comment by anyone else is ignored.
- The newest block wins. A brain revises its vetting by posting a new block, for instance when the facts moved.
- Write the block after a live check of the facts, and use plain words: the operator reads it on a phone.

## Upkeep asks

An ask is upkeep when it carries `loom:factory` or its title starts with `Station:` or `Invariant:` (a lane's verify hold, an invariant, a key read error). The factory path handles upkeep, so it is never a digest item and is not counted in the digest. A brain that decides a person must act adds one more field line to its block:

```
Needs person: yes
```

Only then is the upkeep ask vetted.

## What the digest does with it

- Vetted asks are listed one per line as `repo#N: <question> Recommend: <answer>.`, the 5 newest vetted first, with no count of what is new. With more than 5 vetted, the head says "the 5 newest of N" and links the full list by goal.
- The operator is tagged only when at least one ask is vetted.
- Asks no brain has vetted are not listed. The digest says `N asks are with the brains.` With none vetted it posts only that line and no tag.
- `loom:urgent` asks still go out at once, as before.

The code is `bus/ask_block.py` (the format) and `Station._vet` in `bus/station.py` (the use).
