import io
import unittest
import zipfile
from unittest import mock

import scanner
from scanner import scan


SOURCE = "https://github.com/source/catalog/tree/main/src/Database"


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for path, content in files.items():
            output.writestr(f"catalog-main/{path}", content)
    return buffer.getvalue()


class ScanTests(unittest.TestCase):
    def test_selects_authoritative_sources_and_reports_oversized_files(self):
        result = scan(SOURCE, archive({
            "src/Database/Migrations/2020_CreateOrders.cs": "migrationBuilder.CreateTable(name: \"Orders\");",
            "src/Database/AppDbContext.cs": "class AppDbContext : DbContext {}",
            "src/Database/Tables/Order.cs": "class Order { public int Id { get; set; } }",
            "src/Database/large.sql": "x" * (512 * 1024 + 1),
            "src/Database/AdhocScripts/Manual/backfill.sql": "CREATE TABLE ignored (id int);",
        }))

        self.assertEqual(3, len(result["schemaFiles"]))
        self.assertTrue(result["schemaFiles"][0].endswith("Migrations/2020_CreateOrders.cs"))
        self.assertEqual(
            [{"path": "src/Database/large.sql", "reason": "file_too_large"}],
            result["excludedFiles"],
        )

    def test_ignores_regression_test_directories_and_projects(self):
        base = "https://github.com/source/catalog/tree/main/src"
        result = scan(base, archive({
            "src/Database/AppDbContext.cs": "class AppDbContext : DbContext {}",
            "src/Database/Tables/Order.cs": "class Order { public int Id { get; set; } }",
            "src/Database/RegressionTests/OrderRegression.cs": "migrationBuilder.CreateTable(name: \"Ignored\");",
            "src/Database.RegressionTests/OrderRegression.cs": "migrationBuilder.CreateTable(name: \"Ignored\");",
        }))

        self.assertEqual(
            [
                "https://github.com/source/catalog/blob/main/src/Database/AppDbContext.cs",
                "https://github.com/source/catalog/blob/main/src/Database/Tables/Order.cs",
            ],
            result["schemaFiles"],
        )

    def test_never_selects_files_outside_the_requested_tree(self):
        result = scan(SOURCE, archive({
            "src/Database/Tables/Order.cs": "[Table(\"Orders\")] class Order {}",
            "src/Other/Migrations/CreateOutside.cs": "migrationBuilder.CreateTable(name: \"Outside\");",
        }))

        self.assertEqual(
            ["https://github.com/source/catalog/blob/main/src/Database/Tables/Order.cs"],
            result["schemaFiles"],
        )

    def test_selects_nothing_when_no_file_shows_anything_is_persisted(self):
        # An outer API's "Models" folders hold request/response DTOs, not tables.
        result = scan("https://github.com/source/catalog/tree/main/src", archive({
            "src/Api/Models/LearnerNotification.cs": "public class LearnerNotification { public long NotificationId { get; set; } }",
            "src/Api/Models/Status.cs": "public enum Status { Active, Inactive }",
        }))

        self.assertEqual([], result["schemaFiles"])

    def test_ignores_mock_and_stub_projects(self):
        result = scan("https://github.com/source/catalog/tree/main/src", archive({
            "src/Data/AppDbContext.cs": "class AppDbContext : DbContext {}",
            "src/SFA.DAS.Portal.OuterApi.Mock/Models/Apprentice.cs": "class Apprentice { public string FirstName { get; set; } }",
            "src/SFA.DAS.Portal.Stubs/Models/Calendar.cs": "class Calendar {}",
        }))

        self.assertEqual(
            ["https://github.com/source/catalog/blob/main/src/Data/AppDbContext.cs"],
            result["schemaFiles"],
        )


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
            scanner._download_archive(scanner.parse_source_url("https://github.com/source/catalog/tree/main/src"), "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_downloads_through_the_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/source/catalog/zipball/main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))

    def test_without_a_token_the_public_archive_is_downloaded(self):
        request = self.download(None)
        self.assertEqual("https://codeload.github.com/source/catalog/zip/main", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))
