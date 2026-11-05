# Naming

How Loom issues are named. A label carries an issue's kind; the title says
the outcome. Code decides an issue's kind from its labels, never from a
title prefix, so retitling an issue never changes what the factory does with
it. The rules live in code in bus/naming.py.

## Kinds

| Kind | Label | Title | Length |
|---|---|---|---|
| Goal home | `loom:goal` | `Goal: <outcome>` | 72 at most |
| Backlog goal home | `loom:goal` and `loom:backlog` | `Goal: Loom or a person works every open <repo> issue` | 72 at most |
| Epic | `epic` | `Epic: <outcome>` | 72 at most |
| Map | `wayfinder:map` | `Map: <outcome>` | 72 at most |
| Ticket | `loom:ticket` | an outcome sentence, no kind word | 100 at most (a hard cap) |

- A title carries one leading kind word at most, and only the three above.
  The older goal home prefixes (`Loom goal:`, `Goal home:`, `Wayfinder:`,
  `Backlog:`) and the `[EPIC]` tag are retired.
- An outcome is what is true when the work is done, said as a sentence:
  "Goal: every lane run leaves a transcript", not "Goal: transcripts".
- `bus.brain brief` refuses a ticket title over 100 characters, and
  `bus.brain epic` an epic title over 72 or without `Epic: `.

## Generated tickets

A ticket the station files on its own keeps one fixed lead phrase for its
generator, then `: <detail>` when there is one, and the whole title is cut
on a word to 72 characters with `...`:

| Generator | Title |
|---|---|
| Pull request adoption | `Land or supersede #N: <pull request title>` |
| Deferred review findings | `Settle the review findings left from PR #N` |
| Red default branch, fix forward | `Fix main after merge <sha>: <pull request title>` |
| Red default branch, revert | `Revert <sha>: <pull request title>` |
| Check red before the merge | `Fix pre-existing red check: <check names>` |
| Health intake | `Health: <finding>` |
| Invariant breach | the breach's sentence, one fixed lead per invariant |
| Validation contract | `Add the repository validation contract` |

## Display

The dashboard and the station's reports drop a leading kind word where the
kind is already shown: goal cards and their smaller goals, epic rows, the
/tickets goal groups, the /backlog epic table and the asks digest headings.
They drop old and new kind words alike (`Goal:`, `Loom goal:`,
`Goal home:`, `Backlog:`, `Wayfinder:`, `Map:`, `Epic:`, `[EPIC]`).

## Migration

Backlog goal homes opened before `loom:backlog` existed are titled
`Backlog: ...`. Until each is relabelled and retitled, the station and the
brain still treat a goal home whose title starts with `Backlog:` as a
backlog home, and `bus.brain onboard` adds `loom:backlog` to such a home
instead of opening a second one. An open deferred-findings issue under the
old title (`Deferred review findings from <repo>#N`) still counts, so a pull
request never gets two. Once no open issue uses an old form, the fallbacks
in bus/naming.py and bus/factory.py can go.
