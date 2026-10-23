import io
import unittest
import zipfile
from unittest import mock

import scanner
from scanner import ScanError, parse_source_url, scan

SOURCE = "https://github.com/owner/repo/tree/main/src"


def archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        for path, content in files.items():
            zip_file.writestr(f"repo-main/{path}", content)
    return buffer.getvalue()


class ParseSourceUrlTests(unittest.TestCase):
    def test_parses_owner_repository_ref_and_path(self):
        location = parse_source_url(SOURCE)
        self.assertEqual(location.owner, "owner")
        self.assertEqual(location.repository, "repo")
        self.assertEqual(location.ref, "main")
        self.assertEqual(location.base_path, "src")

    def test_rejects_non_tree_url(self):
        with self.assertRaises(ScanError):
            parse_source_url("https://github.com/owner/repo/blob/main/src/file.cs")


class ScanTests(unittest.TestCase):
    def test_selects_csproj_and_global_json(self):
        result = scan(SOURCE, archive({
            "src/App/App.csproj": "<Project />",
            "src/global.json": "{}",
            "src/README.md": "not relevant",
        }))
        self.assertEqual(
            result["repoMetadataFiles"],
            [
                "https://github.com/owner/repo/blob/main/src/App/App.csproj",
                "https://github.com/owner/repo/blob/main/src/global.json",
            ],
        )
        self.assertEqual(result["excludedFiles"], [])

    def test_ignores_build_and_package_directories(self):
        result = scan(SOURCE, archive({
            "src/App/obj/App.csproj": "<Project />",
            "src/App/bin/Debug/App.csproj": "<Project />",
            "src/packages/Some.Package/Some.Package.csproj": "<Project />",
        }))
        self.assertEqual(result["repoMetadataFiles"], [])

    def test_restricts_to_files_under_requested_path(self):
        result = scan(SOURCE, archive({
            "other/App.csproj": "<Project />",
            "src/App/App.csproj": "<Project />",
        }))
        self.assertEqual(
            result["repoMetadataFiles"],
            ["https://github.com/owner/repo/blob/main/src/App/App.csproj"],
        )

    def test_excludes_non_utf8_files(self):
        result = scan(SOURCE, archive({}) )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zip_file:
            zip_file.writestr("repo-main/src/App/App.csproj", b"\xff\xfe\x00\x01")
        result = scan(SOURCE, buffer.getvalue())
        self.assertEqual(result["repoMetadataFiles"], [])
        self.assertEqual(result["excludedFiles"], [{"path": "src/App/App.csproj", "reason": "not_utf8"}])

    def test_no_matching_files_is_a_valid_empty_result(self):
        result = scan(SOURCE, archive({"src/README.md": "hello"}))
        self.assertEqual(result, {"repoMetadataFiles": [], "excludedFiles": []})



class ArchiveDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"zip"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(scanner.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(scanner.urllib.request, "urlopen", side_effect=urlopen):
            scanner._download_archive(scanner.parse_source_url(SOURCE), "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_downloads_through_the_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/owner/repo/zipball/main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))

    def test_without_a_token_the_public_archive_is_downloaded(self):
        request = self.download(None)
        self.assertEqual("https://codeload.github.com/owner/repo/zip/main", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
