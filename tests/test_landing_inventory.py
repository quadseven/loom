"""Keep the documented endpoint owners in step with the landing code."""
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


class LandingInventoryTest(unittest.TestCase):
    def test_documented_endpoints_match_the_tree(self):
        from bus.landing_inventory import merge_callers

        doc = (ROOT / "docs" / "LANDING.md").read_text(encoding="ascii")
        documented = set(re.findall(r"^\| `([^`]+::[^`]+)` \|", doc, re.MULTILINE))
        self.assertTrue(documented, "expected endpoint rows in docs/LANDING.md")
        self.assertEqual(merge_callers(ROOT), documented)

    def test_reports_python_writes_under_their_qualified_names(self):
        from bus.landing_inventory import merge_callers

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bus").mkdir()
            (root / "lanes").mkdir()
            (root / "bus" / "extra.py").write_text("""
ENABLE = 'mutation { enablePullRequestAutoMerge(input: $input) { clientMutationId } }'

class Landing:
    async def write(self, gh, repo, n):
        path = f'/repos/{repo}/pulls/{n}/merge-async'
        gh.rest('PUT', path)

def adopt(gh):
    gh.graphql(ENABLE)

def poll(gh, repo, n):
    return gh.rest('GET', '/repos/%s/pulls/%d/merge-async' % (repo, n))
""")
            (root / "lanes" / "extra.py").write_text("""
def help_text():
    print("gh pr merge <number>")

def land(pr):
    subprocess.run(['gh', 'pr', 'merge', pr], check=True)
""")
            self.assertEqual(merge_callers(root), {
                "bus/extra.py::Landing.write", "bus/extra.py::adopt", "lanes/extra.py::land",
            })

    def test_reports_shell_writes_including_top_level_commands(self):
        from bus.landing_inventory import merge_callers

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bus").mkdir()
            scripts = root / "lanes" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "extra").write_text("""#!/bin/bash
# Example only: run checks; gh pr merge "$pr".
land() {
    if gh pr merge "$pr"; then return; fi
}
poll() {
    gh api -X GET "repos/$repo/pulls/$pr/merge-async"
}
gh api --method PUT \\
    "repos/$repo/pulls/$pr/merge"
""")
            self.assertEqual(merge_callers(root), {
                "lanes/scripts/extra::land", "lanes/scripts/extra::<script>",
            })
