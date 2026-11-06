import io
import unittest
import urllib.error
import zipfile
from unittest import mock

import github_archive


def archive_of(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(f"owner-repo-1a2b3c/{path}", content)
    return buffer.getvalue()


class ReadFilesTests(unittest.TestCase):
    def read(self, paths, files, **kwargs):
        return github_archive.read_files(
            "owner", "repo", "main", paths, tenant=None, user_agent="test", max_file_bytes=kwargs.pop("max_file_bytes", 100),
            archive_bytes=archive_of(files), **kwargs,
        )

    def test_returns_only_the_requested_files(self):
        files = self.read(["src/a.cs", "src/b.cs"], {"src/a.cs": "a", "src/b.cs": "b", "src/c.cs": "c"})

        self.assertEqual({"src/a.cs": b"a", "src/b.cs": b"b"}, files)

    def test_a_missing_file_is_unavailable(self):
        with self.assertRaises(github_archive.ArchiveError) as raised:
            self.read(["src/missing.cs"], {"src/a.cs": "a"})

        self.assertEqual("source_unavailable", raised.exception.code)

    def test_a_requested_file_over_the_limit_is_too_large(self):
        with self.assertRaises(github_archive.ArchiveError) as raised:
            self.read(["src/big.cs"], {"src/big.cs": "x" * 101})

        self.assertEqual("source_too_large", raised.exception.code)

    def test_an_unrequested_file_over_the_limit_is_ignored(self):
        self.assertEqual({"src/a.cs": b"a"}, self.read(["src/a.cs"], {"src/a.cs": "a", "src/big.cs": "x" * 101}))

    def test_a_corrupt_archive_is_invalid(self):
        with self.assertRaises(github_archive.ArchiveError) as raised:
            github_archive.read_files("owner", "repo", "main", ["a"], tenant=None, user_agent="test", max_file_bytes=10, archive_bytes=b"not a zip")

        self.assertEqual("invalid_source", raised.exception.code)


class DownloadTests(unittest.TestCase):
    def test_a_transient_server_error_is_retried(self):
        body = archive_of({"src/a.cs": "a"})

        class Response:
            headers = {"Content-Length": str(len(body))}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return body

        responses = [urllib.error.HTTPError("url", 502, "Bad Gateway", {}, None), Response()]

        def urlopen(request, timeout):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch.object(github_archive.github_app, "read_token", return_value=None), \
                mock.patch.object(github_archive.urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(github_archive.time, "sleep"):
            files = github_archive.read_files("owner", "repo", "main", ["src/a.cs"], tenant=None, user_agent="test", max_file_bytes=10)

        self.assertEqual({"src/a.cs": b"a"}, files)

    def test_a_rate_limit_is_not_retried(self):
        calls = []

        def urlopen(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError("url", 403, "Forbidden", {}, None)

        with mock.patch.object(github_archive.github_app, "read_token", return_value="token"), \
                mock.patch.object(github_archive.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaisesRegex(github_archive.ArchiveError, "HTTP 403"):
                github_archive.read_files("owner", "repo", "main", ["src/a.cs"], tenant="t/r", user_agent="test", max_file_bytes=10)

        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
