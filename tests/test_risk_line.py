"""The Risk line and tier label of a lane pull request (bus/risk_line.py)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import hunt_plan, labels, merge_tier, pr_repair, risk_line  # noqa: E402

REPO = "example-owner/example-repo"
ZERO = "0" * 40


def raw(*rows):
    """`git diff --raw` lines: (status, path, new mode)."""
    return "\n".join(":100644 %s %s %s %s\t%s" % (
        row[2] if len(row) > 2 else "100644", ZERO, ZERO, row[0], row[1]) for row in rows)


def computed(diff, summary="", brief="", sensitive=False):
    return risk_line.compute(REPO, risk_line.entries(diff), summary, brief, sensitive)


LANE_TWO_WAY = "Fix it\n\n## Risk\nDoor: two-way\nBlast radius: repo\n"


class Door(unittest.TestCase):
    def test_a_migration_is_one_way_even_when_the_lane_wrote_two_way(self):
        found = computed(raw(("A", "db/migrations/0007_add.sql")), LANE_TWO_WAY)
        self.assertEqual(found.door, risk_line.ONE_WAY)
        self.assertEqual(risk_line.line(found),
                         "Risk: app-code, one-way door, blast radius repo")

    def test_moving_a_migration_out_of_its_folder_is_one_way(self):
        diff = ":100644 100644 %s %s R100\tdb/migrations/1.sql\tbus/old.sql" % (ZERO, ZERO)
        self.assertEqual(computed(diff, LANE_TWO_WAY).door, risk_line.ONE_WAY)

    def test_a_deleted_data_file_is_one_way(self):
        found = computed(raw(("D", "data/seed.csv")), LANE_TWO_WAY)
        self.assertEqual(found.door, risk_line.ONE_WAY)

    def test_a_deleted_test_fixture_or_source_file_keeps_the_lanes_word(self):
        for path in ("tests/fixtures/seed.json", "bus/old.py"):
            self.assertEqual(computed(raw(("D", path)), LANE_TWO_WAY).door, risk_line.TWO_WAY)

    def test_a_listed_operator_step_is_one_way(self):
        summary = LANE_TWO_WAY + "\n## %s\nResize the disk\n" % pr_repair.OPERATOR_SECTION
        self.assertEqual(computed(raw(("M", "bus/a.py")), summary).door, risk_line.ONE_WAY)
        brief = "## %s\nResize the disk\n" % pr_repair.OPERATOR_SECTION
        self.assertEqual(computed(raw(("M", "bus/a.py")), LANE_TWO_WAY, brief).door,
                         risk_line.ONE_WAY)

    def test_no_operator_step_said_in_any_form_is_no_step(self):
        for word in ("none", "None.", "none stated"):
            summary = LANE_TWO_WAY + "\n## %s\n%s\n" % (pr_repair.OPERATOR_SECTION, word)
            self.assertEqual(computed(raw(("M", "bus/a.py")), summary).door, risk_line.TWO_WAY)

    def test_the_operator_step_read_matches_the_merge_gate(self):
        for word in ("- none", "nothing"):
            summary = LANE_TWO_WAY + "\n## %s\n%s\n" % (pr_repair.OPERATOR_SECTION, word)
            self.assertEqual(computed(raw(("M", "bus/a.py")), summary).door, risk_line.TWO_WAY, word)
        said = "This needs a data migration on the live volumes."
        self.assertEqual(computed(raw(("M", "bus/a.py")), LANE_TWO_WAY, said).door,
                         risk_line.ONE_WAY)

    def test_a_doc_or_module_about_migrations_is_not_a_migration(self):
        for path in ("docs/migration-notes.md", "bus/label_migration.py"):
            self.assertEqual(computed(raw(("M", path)), LANE_TWO_WAY).door, risk_line.TWO_WAY)
        self.assertEqual(computed(raw(("A", "db/0001_migration.sql")), LANE_TWO_WAY).door,
                         risk_line.ONE_WAY)

    def test_a_data_file_named_like_a_test_outside_a_test_folder_is_data(self):
        self.assertEqual(computed(raw(("D", "data/testimonials.csv")), LANE_TWO_WAY).door,
                         risk_line.ONE_WAY)

    def test_a_lane_that_writes_nothing_is_door_unstated(self):
        found = computed(raw(("M", "bus/a.py")), "Fix it\n")
        self.assertEqual(risk_line.line(found),
                         "Risk: app-code, door unstated, blast radius unstated")

    def test_a_word_outside_the_two_doors_is_unstated_never_two_way(self):
        for summary in ("## Risk\nDoor: probably fine\n", "## Risk\nDoor: two-way-ish\n"):
            self.assertEqual(computed(raw(("M", "bus/a.py")), summary).door, risk_line.UNSTATED)

    def test_the_blast_radius_is_one_plain_word(self):
        for said, want in (("repo", "repo"), ("the whole fleet", "unstated"),
                           ("<b>x</b>", "unstated"), ("#12", "unstated")):
            summary = "## Risk\nDoor: two-way\nBlast radius: %s\n" % said
            self.assertEqual(computed(raw(("M", "bus/a.py")), summary).blast, want)


class Tier(unittest.TestCase):
    CASES = ((merge_tier.DOCS_TESTS, ("M", "docs/a.md")),
             (merge_tier.APP_CODE, ("M", "bus/a.py")),
             (merge_tier.DEPLOY_CONFIG, ("M", "k8s/app.yaml")),
             (merge_tier.SECURITY, ("M", ".github/workflows/a.yml")),
             (merge_tier.CROSS_REPO, ("M", "pin.env")))

    def test_the_label_matches_merge_tier_for_each_tier(self):
        self.assertEqual(sorted(t for t, _ in self.CASES), sorted(merge_tier.TIERS))
        for tier, row in self.CASES:
            found = computed(raw(row))
            want = merge_tier.classify(REPO, [{"filename": row[1]}], "", False)[0]
            self.assertEqual((found.tier, want), (tier, tier))
            self.assertEqual(risk_line.label(found), "tier:" + tier)
            self.assertIn("tier:" + tier, labels.LABELS)

    def test_a_submodule_gitlink_is_cross_repo(self):
        self.assertEqual(computed(raw(("M", "vendor/sub", "160000"))).tier, merge_tier.CROSS_REPO)

    def test_a_rename_keeps_the_path_it_had(self):
        diff = ":100644 100644 %s %s R100\tk8s/app.yaml\ttests/app.yaml" % (ZERO, ZERO)
        self.assertEqual(computed(diff).tier, merge_tier.DEPLOY_CONFIG)

    def test_the_sensitive_class_and_a_ticket_naming_another_repo_lift_the_tier(self):
        row = ("M", "bus/a.py")
        self.assertEqual(computed(raw(row), sensitive=True).tier, merge_tier.SECURITY)
        self.assertEqual(computed(raw(row), brief="Touches example-owner/other-repo too.").tier,
                         merge_tier.CROSS_REPO)

    def test_context_the_runner_added_to_a_brief_does_not_lift_the_tier(self):
        brief = "## What\nFix it.\n\n## Linked pull requests\n%s#%d, open: x\n" % (
            "example-owner/other-repo", 5)
        self.assertEqual(computed(raw(("M", "bus/a.py")), brief=brief).tier, merge_tier.APP_CODE)

    def test_tier_labels_are_managed(self):
        self.assertIn("tier:", labels.MANAGED_PREFIXES)


class Body(unittest.TestCase):
    INTENT = "## Intent\nClass: logic\n- Fix it\nFiles: 1 logic"

    def test_the_line_sits_in_intent_under_the_class(self):
        out = risk_line.with_risk(self.INTENT, "Risk: app-code, door unstated, "
                                               "blast radius unstated")
        self.assertEqual(out.split("\n")[:3],
                         ["## Intent", "Class: logic",
                          "Risk: app-code, door unstated, blast radius unstated"])

    def test_cli_prints_the_intent_then_the_label(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            def put(name, text):
                path = Path(tmp) / name
                path.write_text(text)
                return str(path)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = risk_line.main([
                    "--repo", REPO, "--class", "logic", "--intent", put("i", self.INTENT),
                    "--raw", put("r", raw(("A", "migrations/1.sql"))),
                    "--summary", put("s", LANE_TWO_WAY), "--brief", put("b", "")])
        lines = out.getvalue().strip().split("\n")
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "tier:app-code")
        self.assertIn("Risk: app-code, one-way door, blast radius repo", lines)
