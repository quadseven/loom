"""Test for pin updater bug fix: pin branch should not equal base after update."""
import unittest
import base64
import hashlib
from unittest.mock import MagicMock

from bus.pin import pin_one


class TestPinOneUpdate(unittest.TestCase):
    def test_content_called_before_and_after_force_patch(self):
        """Test that content is called before force-patching and after."""
        gh = MagicMock()
        spec = {
            "repo": "example/repo",
            "path": "dashboard.py",
            "revision_var": "LOOM_VERSION",
            "sha256_var": "LOOM_SHA256",
        }
        base = "main"
        branch = "loom/pin-" + hashlib.sha256(spec["path"].encode()).hexdigest()[:12]
        branch_encoded = branch.replace("/", "%2F")
        revision = "abc123def456789"
        old = "def456789012"
        digest = "sha256:digest123"
        log = MagicMock()
        
        # Track all calls to gh.rest
        rest_calls = []
        
        def mock_rest(method, *args, **kwargs):
            endpoint = args[0] if args else ""
            response = MagicMock()
            
            if endpoint == "/repos/example/repo":
                response.data = {"default_branch": "main"}
            elif "contents" in endpoint:
                response.data = {
                    "content": base64.b64encode("LOOM_VERSION=old\nLOOM_SHA256=old123".encode()).decode(),
                    "sha": "file123"
                }
                rest_calls.append(("GET", endpoint, kwargs))
            elif "git/ref/heads" in endpoint:
                if branch in endpoint:
                    response.data = {"sha": "base123"}
                else:
                    response.data = {"object": {"sha": "base123"}}
                    if "404" in str(kwargs.get("ok", [])):
                        response.ok = (404,)
            else:
                response.data = {"default_branch": "main"}
            
            rest_calls.append(("GET", endpoint, kwargs))
            return response
        
        gh.rest.side_effect = mock_rest
        gh.paginate = MagicMock(return_value=[])
        
        pin_one(gh, "quadseven/loom", revision, spec, lambda: digest, log)
        
        # Find content calls
        content_calls = [call for call in rest_calls if call[0] == 'GET' and 'contents' in call[1]]
        
        # Verify that content was called at least once before and after force-patch
        content_calls_with_ref = [call for call in content_calls if branch_encoded in call[1]]
        self.assertGreaterEqual(len(content_calls_with_ref), 1, "Should have content calls with ref (after force-patch)")
        
        content_calls_without_ref = [call for call in content_calls if branch_encoded not in call[1]]
        self.assertGreaterEqual(len(content_calls_without_ref), 1, "Should have content calls without ref (before force-patch)")


if __name__ == "__main__":
    unittest.main()
