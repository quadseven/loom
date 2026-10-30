"""Which ticket boxes a merged pull request left unbuilt (loom#2026).

A merge moves its ticket to loom:merged only when every code box is built;
the unbuilt list names only boxes the pull request left. The fixtures below
are the acceptance sections of two real tickets and the pull requests merged
for them: one built 1 of 4 boxes and marked the rest not built, yet its ticket
went to loom:merged; the other built 2 boxes the ticket already had ticked,
yet the requeue note named all four.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import factory  # noqa: E402

# A ticket whose third box the pull request built, with the box's backticks
# dropped, and whose first two boxes it marked not built.
SAMPLER_TICKET = """## Acceptance criteria
- [ ] Audit endpoint or job reports all six fields for every online team and roster member.
- [ ] Per-member `app.dev.*` metrics emitted; digest-tier watches fire on stalled >15 min, >6 errors/hour, or a score 10+ below target.
- [ ] `digest_sample` includes rows for the second roster (A, B, C, D, E) in addition to existing rosters.
- [ ] Live: first `app.dev.audit` run on the dev site lists stalled members and empty-slot members found manually, with nothing extra.

## Size
M
"""

SAMPLER_PULL = """## Acceptance criteria
- [ ] Audit endpoint or job reports all six fields for every online team and roster member; not built: unavailable in this checkout.
- [ ] Per-member app.dev.* metrics emitted and digest-tier watches fire at the requested thresholds; not built: unavailable in this checkout.
- [x] digest_sample includes rows for the second roster (A, B, C, D, E) in addition to existing rosters.
- [ ] Live: first app.dev.audit run lists manually confirmed stalled members and empty-slot members with nothing extra; not built: no audit job was implemented or run live.

Size: M
"""

# A ticket whose first two boxes were ticked before its pull request merged;
# the pull request listed its work as plain bullets.
ENCOUNTER_TICKET = """## Acceptance criteria
- [x] Stage table with all ten stages listing required workers, retries, rate limits, timeouts, and quotas
- [x] Capacity calculator that reports what the pool lacks for each stage based on stage requirements
- [ ] Updated pool layout module replacing fixed group template with stage-driven capacity
- [ ] Validation that capacity satisfies actual stage needs from this server's job data

## Size
M
"""

ENCOUNTER_PULL = """## Acceptance criteria
- Stage table with all ten stages listing workers, retries, rate limits, timeouts, backoff and quotas, each citing the pinned stage config (`tests/test_stages.py` TheTableCoversEveryStage)
- Calculator reports what the pool lacks for each stage, from slots for workers and kinds for the rest (TheCalculatorReportsWhatIsMissing)
- Approved layouts carry the report without moving a slot; automatic layouts carry it too (TheLayoutCarriesTheReport)
- Full suite as CI runs it, OK

Size: S
"""


def boxes(body):
    return [c.partition(" ")[2] for c in factory.acceptance_criteria(body)]


class UnbuiltCriteriaTest(unittest.TestCase):
    TICKET = ("## Acceptance criteria\n- [ ] First works; test.\n- [ ] Second works.\n"
              "- [ ] Third works.\n- [ ] Fourth works.\n")

    def test_one_of_four_built_names_the_three_left(self):
        pull = ("## Acceptance criteria\n- [x] First works; test.\n"
                "- [ ] Second works.; not built: needs a key\n- [ ] Third works.\n")
        self.assertEqual(factory.unbuilt_criteria(self.TICKET, [pull]),
                         ["Second works.", "Third works.", "Fourth works."])

    def test_a_box_marked_not_built_is_unbuilt(self):
        pull = ("## Acceptance criteria\n- [x] First works; test.\n- [x] Second works.\n"
                "- [x] Third works.\n- [ ] Fourth works.; not built: needs a key\n")
        self.assertEqual(factory.unbuilt_criteria(self.TICKET, [pull]), ["Fourth works."])

    def test_whitespace_and_markup_do_not_hide_a_built_box(self):
        ticket = "## Acceptance criteria\n- [ ] The `run_check` helper  reads **every** [file](a.md).\n"
        pull = "## Acceptance criteria\n- [x] The run_check helper reads every file\n"
        self.assertEqual(factory.unbuilt_criteria(ticket, [pull]), [])

    def test_a_box_any_merged_pull_request_built_is_not_named(self):
        first = "## Acceptance criteria\n- [x] First works; test.\n- [x] Second works.\n"
        second = "## Acceptance criteria\n- [x] Third works.\n"
        self.assertEqual(factory.unbuilt_criteria(self.TICKET, [first, second]),
                         ["Fourth works."])

    def test_a_box_already_ticked_on_the_ticket_is_not_named(self):
        ticket = self.TICKET.replace("- [ ] Second", "- [x] Second")
        self.assertEqual(factory.unbuilt_criteria(ticket, [""]),
                         ["First works; test.", "Third works.", "Fourth works."])

    def test_the_sampler_fixture_leaves_its_ticket_unbuilt(self):
        # The second box (metrics emitted, watches fire) and the Live box are
        # live boxes, which the verify path proves; the first is code.
        ticket = boxes(SAMPLER_TICKET)
        left = factory.unbuilt_criteria(SAMPLER_TICKET, [SAMPLER_PULL])
        self.assertEqual(left, ticket[:1])
        self.assertNotIn(ticket[2], left)

    def test_the_encounter_fixture_names_only_the_boxes_the_pull_request_left(self):
        ticket = boxes(ENCOUNTER_TICKET)
        left = factory.unbuilt_criteria(ENCOUNTER_TICKET, [ENCOUNTER_PULL])
        self.assertEqual(left, ticket[2:])

    def test_a_built_box_never_appears_in_either_fixture(self):
        for ticket, pull in ((SAMPLER_TICKET, SAMPLER_PULL), (ENCOUNTER_TICKET, ENCOUNTER_PULL)):
            built = {factory.criterion_key(c.partition(" ")[2])
                     for c in factory.acceptance_criteria(pull) if c.startswith("x ")}
            built |= {factory.criterion_key(c.partition(" ")[2])
                      for c in factory.acceptance_criteria(ticket) if c.startswith("x ")}
            for text in factory.unbuilt_criteria(ticket, [pull]):
                self.assertNotIn(factory.criterion_key(text), built)


if __name__ == "__main__":
    unittest.main()
