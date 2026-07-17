import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import sync_cloudflare_hugo  # noqa: E402


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"success": true, "result": {}}'


class CloudflareSyncTests(unittest.TestCase):
    def test_retries_transient_522_then_succeeds(self):
        error = urllib.error.HTTPError("https://api.cloudflare.com", 522, "timeout", {}, io.BytesIO())
        with patch.object(sync_cloudflare_hugo.urllib.request, "urlopen", side_effect=[error, Response()]), \
            patch.object(sync_cloudflare_hugo.time, "sleep") as sleep, \
            patch("builtins.print"):
            result = sync_cloudflare_hugo.api_request("https://api.cloudflare.com", "token")
        self.assertTrue(result["success"])
        sleep.assert_called_once_with(1.0)

    def test_does_not_retry_permanent_authentication_errors(self):
        error = urllib.error.HTTPError("https://api.cloudflare.com", 403, "forbidden", {}, io.BytesIO())
        with patch.object(sync_cloudflare_hugo.urllib.request, "urlopen", side_effect=error), \
            patch.object(sync_cloudflare_hugo.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                sync_cloudflare_hugo.api_request("https://api.cloudflare.com", "token")
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
