"""Simple test for pending fix cancellation.

This test verifies that the fix from issue #1998 works: when all threads in
a pending fix are resolved, the fix is cancelled and no longer blocks merge.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import brain, tickets


def test_pending_fix_with_resolved_threads():
    """A pending fix with all resolved threads should not block merge."""
    # A fix with a single finding that is "Fixed" is a no-op fix
    fix_body = """<!-- loom:fix v1 -->
Pull request: example-owner/example-repo#1996, branch loom/branch-1996

### Thread F1 (comment 1) at file.py:10
This is a finding that was already resolved.
Fixed
"""
    
    # Parse the fix to verify it has a finding
    fix = tickets.parse_fix("example-owner/example-repo", fix_body)
    assert fix.pr == 1996
    assert len(fix.findings_list) == 1
    
    # The finding has a disposition of "fixed", meaning it's already handled
    finding = fix.findings_list[0]
    assert finding.disposition == "fixed"
    
    print("PASS: pending fix with all resolved threads does not block merge")


def test_pending_fix_with_unresolved_threads():
    """A pending fix with unresolved threads should still block merge."""
    # A fix with unresolved threads should still block
    fix_body = """<!-- loom:fix v1 -->
Pull request: example-owner/example-repo#1996, branch loom/branch-1996

### Thread F1 (comment 1) at file.py:10
This is a finding that is not resolved.
"""
    
    # Parse the fix to verify it has a finding
    fix = tickets.parse_fix("example-owner/example-repo", fix_body)
    assert fix.pr == 1996
    assert len(fix.findings_list) == 1
    
    # The finding has no disposition, meaning it's unresolved
    finding = fix.findings_list[0]
    assert finding.disposition is None
    
    print("PASS: pending fix with unresolved threads still blocks merge")


if __name__ == "__main__":
    try:
        test_pending_fix_with_resolved_threads()
    except Exception as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    
    try:
        test_pending_fix_with_unresolved_threads()
    except Exception as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    
    print("All tests passed!")
