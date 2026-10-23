import os
import time
import unittest
from unittest import mock

import github_app


def expiry(seconds_from_now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds_from_now))


class GitHubAppTests(unittest.TestCase):
    def setUp(self):
        github_app._cache.clear()
        self.calls = []

        def call(method, path, bearer, body=None):
            self.calls.append((method, path, body))
            if path.endswith("/installation"):
                return {"id": 42 if path.startswith("/repos/tenant-a/") else 99}
            return {"token": f"token-{len(self.calls)}", "expires_at": expiry(3600)}

        patches = [
            mock.patch.object(github_app, "_call", side_effect=call),
            mock.patch.object(github_app, "_app_jwt", return_value="app-jwt"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_without_the_app_the_personal_access_tokens_are_used(self):
        with mock.patch.dict(os.environ, {"GITHUB_READ_TOKEN": "read-pat", "GITHUB_PR_TOKEN": "pr-pat"}, clear=True):
            self.assertEqual("read-pat", github_app.read_token("tenant-a/atlas"))
            self.assertEqual("pr-pat", github_app.write_token("tenant-a/atlas"))
        self.assertEqual([], self.calls)

    def test_read_token_is_minted_for_the_tenants_installation(self):
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "pem", "GITHUB_READ_TOKEN": "read-pat"}, clear=True):
            self.assertEqual("token-2", github_app.read_token("tenant-a/atlas"))
            # Without a tenant there is no installation to use, so the agent keeps its configured token.
            self.assertEqual("read-pat", github_app.read_token(None))
        self.assertEqual(("GET", "/repos/tenant-a/atlas/installation", None), self.calls[0])
        self.assertEqual(("POST", "/app/installations/42/access_tokens", {"permissions": github_app.READ_PERMISSIONS}), self.calls[1])

    def test_write_token_is_limited_to_the_data_repository(self):
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "pem"}, clear=True):
            github_app.write_token("tenant-b/catalogue")
        self.assertEqual(
            ("POST", "/app/installations/99/access_tokens", {"permissions": github_app.WRITE_PERMISSIONS, "repositories": ["catalogue"]}),
            self.calls[1],
        )

    def test_tokens_are_cached_per_tenant_until_close_to_expiry(self):
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "pem"}, clear=True):
            first = github_app.read_token("tenant-a/atlas")
            self.assertEqual(first, github_app.read_token("tenant-a/atlas"))
            self.assertNotEqual(first, github_app.read_token("tenant-b/atlas"))
            github_app._cache[("tenant-a/atlas", (), tuple(sorted(github_app.READ_PERMISSIONS.items())))] = ("old", time.time() + 60)
            self.assertNotEqual("old", github_app.read_token("tenant-a/atlas"))

    def test_tenant_must_be_a_repository(self):
        with mock.patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "pem"}, clear=True):
            with self.assertRaises(github_app.GitHubAppError):
                github_app.read_token("tenant-a")


if __name__ == "__main__":
    unittest.main()
