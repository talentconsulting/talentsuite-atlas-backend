import io
import unittest
import zipfile
from unittest import mock

import scanner
from scanner import scan


SOURCE = "https://github.com/source/app/tree/main/src"


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for path, content in files.items():
            output.writestr(f"app-main/{path}", content)
    return buffer.getvalue()


class ScanTests(unittest.TestCase):
    def test_selects_architecture_evidence(self):
        result = scan(SOURCE, archive({
            "src/Program.cs": "var builder = WebApplication.CreateBuilder(args); builder.Services.AddDbContext<AppDbContext>();",
            "src/OrdersController.cs": "class OrdersController : ControllerBase { }",
            "src/appsettings.json": '{"ConnectionStrings":{"Sql":"redacted"}}',
            "src/Domain/Order.cs": "class Order { public long Id { get; set; } }",
        }))

        paths = result["sourceFiles"]
        self.assertTrue(any(path.endswith("src/Program.cs") for path in paths))
        self.assertTrue(any(path.endswith("src/OrdersController.cs") for path in paths))
        self.assertTrue(any(path.endswith("src/appsettings.json") for path in paths))
        self.assertFalse(any(path.endswith("src/Domain/Order.cs") for path in paths))

    def test_selects_infra_and_dependency_files(self):
        result = scan(SOURCE, archive({
            "src/app.csproj": "<Project><PackageReference Include=\"Microsoft.EntityFrameworkCore\" /></Project>",
            "src/infra/main.bicep": "resource database 'Microsoft.Sql/servers/databases@2022-05-01-preview' = {}",
            "src/docker-compose.yml": "services:\n  api:\n    image: app",
            "other/Program.cs": "WebApplication.CreateBuilder(args);",
        }))

        self.assertEqual([
            "https://github.com/source/app/blob/main/src/docker-compose.yml",
            "https://github.com/source/app/blob/main/src/app.csproj",
            "https://github.com/source/app/blob/main/src/infra/main.bicep",
        ], result["sourceFiles"])

    def test_ignores_tests_and_files_outside_requested_tree(self):
        result = scan(SOURCE, archive({
            "src/Program.cs": "WebApplication.CreateBuilder(args);",
            "src/Tests/TestServer.cs": "WebApplication.CreateBuilder(args);",
            "other/Program.cs": "WebApplication.CreateBuilder(args);",
        }))

        self.assertEqual(["https://github.com/source/app/blob/main/src/Program.cs"], result["sourceFiles"])

    def test_ignores_dotted_dotnet_test_project_folders(self):
        result = scan(SOURCE, archive({
            "src/App.UnitTests/OrdersControllerTests.cs": "class OrdersControllerTests { Controller c; }",
        }))

        self.assertEqual([], result["sourceFiles"])

    def test_reports_oversized_candidate(self):
        result = scan(SOURCE, archive({
            "src/Program.cs": "WebApplication.CreateBuilder(args);" + "x" * (512 * 1024),
        }))

        self.assertEqual([], result["sourceFiles"])
        self.assertEqual([{"path": "src/Program.cs", "reason": "file_too_large"}], result["excludedFiles"])


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
            scanner._download_archive(scanner.parse_source_url("https://github.com/source/app/tree/main/src"), "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_downloads_through_the_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/source/app/zipball/main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))

    def test_without_a_token_the_public_archive_is_downloaded(self):
        request = self.download(None)
        self.assertEqual("https://codeload.github.com/source/app/zip/main", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
