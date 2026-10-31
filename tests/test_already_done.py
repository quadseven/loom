"""A box that says its work already exists must cite code on the base."""
import base64
import unittest
from types import SimpleNamespace

from bus import already_done


def body(*boxes):
    return "## Acceptance criteria\n" + "\n".join(boxes) + "\n"


FILES = {"bus/proof.py": "import re\n\n\ndef criteria(body):\n    return []\n"}


class GhStub:
    def __init__(self, status=200):
        self.status, self.paths = status, []

    def rest(self, method, path, body=None, ok=()):
        self.paths.append(path)
        if self.status != 200:
            return SimpleNamespace(data={"message": "Not Found"})
        text = FILES["bus/proof.py"].encode()
        return SimpleNamespace(data={"encoding": "base64",
                                     "content": base64.b64encode(text).decode()})


class AlreadyDoneTest(unittest.TestCase):
    def test_the_words_alone_are_no_proof(self):
        boxes = body("- [ ] Move every route; not built: already in tree",
                     "- [ ] Count the names; not built: already satisfied by prior state")
        self.assertEqual(len(already_done.uncited(boxes)), 2)
        self.assertEqual(len(already_done.unproven(boxes, FILES.get)), 2)

    def test_a_reason_that_claims_nothing_is_not_read(self):
        boxes = body("- [ ] Move a route; not built: requires a later integration",
                     "- [x] Tick a box")
        self.assertEqual(already_done.uncited(boxes), [])
        self.assertEqual(already_done.unproven(boxes, FILES.get), [])

    def test_a_ticket_box_about_the_words_is_not_a_claim(self):
        boxes = body("- [x] A box saying already in tree must cite code")
        self.assertEqual(already_done.uncited(boxes), [])

    def test_each_citation_form_resolves_on_the_base(self):
        for cite in ("bus/proof.py:4", "bus/proof.py#L4", "bus/proof.py:4-5",
                     "bus/proof.py::criteria", "`bus/proof.py` `criteria`",
                     "`bus/proof.py:4`", "bus/proof.py::proof.criteria",
                     "bus/proof.py::criteria."):
            boxes = body("- [ ] A; not built: already in tree at %s" % cite)
            self.assertEqual(already_done.uncited(boxes), [], cite)
            self.assertEqual(already_done.unproven(boxes, FILES.get), [], cite)

    def test_a_citation_the_base_cannot_back_is_no_proof(self):
        for cite in ("bus/absent.py:4", "bus/proof.py:99", "bus/proof.py::absent",
                     "bus/proof.py::crit", "bus/proof.py::absent.",
                     "`bus/proof.py` `absent.`"):
            boxes = body("- [ ] A; not built: already in tree at %s" % cite)
            self.assertEqual(already_done.uncited(boxes), [], cite)
            self.assertEqual(len(already_done.unproven(boxes, FILES.get)), 1, cite)

    def test_a_path_with_no_line_or_symbol_is_no_citation(self):
        boxes = body("- [ ] A; not built: already in tree, see bus/proof.py")
        self.assertEqual(len(already_done.uncited(boxes)), 1)

    def test_the_base_reader_reads_the_base_branch_once_per_path(self):
        gh = GhStub()
        read = already_done.base_reader(gh, "o/r", {"base": {"ref": "main", "sha": "abc"}})
        self.assertEqual(read("bus/proof.py"), FILES["bus/proof.py"])
        read("bus/proof.py")
        self.assertEqual(gh.paths, ["/repos/o/r/contents/bus/proof.py?ref=main"])

    def test_the_base_reader_refuses_a_path_that_leaves_the_repo(self):
        gh = GhStub(404)
        read = already_done.base_reader(gh, "o/r", {"base": {"ref": "main"}})
        self.assertIsNone(read("bus/../../../other/contents/a.py"))
        self.assertEqual(gh.paths, [])

    def test_the_base_reader_reads_a_missing_file_as_none(self):
        read = already_done.base_reader(GhStub(404), "o/r", {"base": {"ref": "main"}})
        self.assertIsNone(read("bus/absent.py"))
