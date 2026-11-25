"""templates/business-repo: the kit has the design layout and five kind templates."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "templates" / "business-repo"
KINDS = KIT / ".loom" / "kinds"

FRONT_KEYS = [
    "kind", "title", "question", "status", "supersedes", "created",
    "author_lane", "confidence", "confidence_reason", "regulatory", "entities",
]
SECTIONS = {
    "research": ["Summary", "Question and scope", "Findings",
                 "Estimates and assumptions", "Confidence", "Not verified",
                 "Sources", "Next questions"],
    "study": ["Summary", "Subject and why it matters",
              "Offer, positioning and pricing", "Channels and messaging",
              "Strengths and gaps against us", "Confidence", "Not verified",
              "Sources"],
    "memo": ["Decision needed", "Options", "Criteria", "Evidence per option",
             "Recommendation with confidence", "Risks", "Disagreements",
             "Not verified", "Sources", "Owner decision"],
    "outreach": ["Why this recipient", "Message", "What we ask for",
                 "Facts used in the message", "Notes for the sender",
                 "Follow-up plan", "Not verified"],
    "plan": ["Goal and metric", "Audience", "Channels and tactics", "Budget",
             "Timeline", "Risks", "Assumptions to test", "What we will not do",
             "Sources", "Not verified"],
}
OUTREACH_KEYS = ["recipient", "contact_route", "purpose", "channel", "approval"]
FOLDERS = ["questions", "research", "competitors", "brand", "memos", "plans",
           "outbox/drafts", "outbox/sent", "digests", "scripts", ".github"]
FILES = ["README.md", "AGENTS.md", ".loom/validation.conf",
         ".loom/sensitive-paths"]


def parse(path):
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert m, f"{path.name}: no front matter"
    keys = [ln.split(":", 1)[0] for ln in m.group(1).splitlines()
            if re.match(r"^[a-z_]+:", ln)]
    values = dict(ln.split(":", 1) for ln in m.group(1).splitlines()
                  if re.match(r"^[a-z_]+:", ln))
    sections = re.findall(r"^## (.+)$", m.group(2), re.M)
    return keys, values, sections


class BusinessRepoKit(unittest.TestCase):
    def test_layout(self):
        for name in FILES:
            self.assertTrue((KIT / name).is_file(), name)
        for name in FOLDERS:
            self.assertTrue((KIT / name / ".gitkeep").is_file(), name)

    def test_sensitive_paths(self):
        lines = (KIT / ".loom/sensitive-paths").read_text().split()
        self.assertEqual(lines, ["outbox/", ".github/"])

    def test_one_template_per_kind(self):
        self.assertEqual(sorted(p.stem for p in KINDS.glob("*.md")),
                         sorted(SECTIONS))

    def test_kind_templates_parse(self):
        for kind, sections in SECTIONS.items():
            keys, values, found = parse(KINDS / f"{kind}.md")
            want = FRONT_KEYS + (OUTREACH_KEYS if kind == "outreach" else [])
            self.assertEqual(keys, want, kind)
            self.assertEqual(values["kind"].strip(), kind)
            self.assertEqual(found, sections, kind)

    def test_lane_status_is_draft_or_proposed(self):
        for kind in SECTIONS:
            _, values, _ = parse(KINDS / f"{kind}.md")
            want = "proposed" if kind == "memo" else "draft"
            self.assertEqual(values["status"].strip(), want, kind)

    def test_outreach_approval_is_empty(self):
        _, values, _ = parse(KINDS / "outreach.md")
        self.assertEqual(values["approval"].strip(), "")


if __name__ == "__main__":
    unittest.main()
