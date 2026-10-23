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
    def test_selects_docker_compose_and_env_example_unconditionally(self):
        result = scan(SOURCE, archive({
            "src/docker-compose.yml": "version: '3'\nservices:\n  web:\n    image: nginx\n",
            "src/.env.example": "SOME_KEY=value\n",
        }))

        paths = result["localDevConfigFiles"]
        self.assertEqual(2, len(paths))
        self.assertTrue(any(path.endswith("src/docker-compose.yml") for path in paths))
        self.assertTrue(any(path.endswith("src/.env.example") for path in paths))
        self.assertEqual([], result["excludedFiles"])

    def test_selects_config_named_files_unconditionally(self):
        # No registration-style content at all -- this is what would catch a regression back to
        # service-dependency-source-discovery's stricter "filename AND content evidence" behaviour.
        result = scan(SOURCE, archive({
            "src/appsettings.json": '{"Logging":{"LogLevel":{"Default":"Information"}}}',
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/appsettings.json"],
            result["localDevConfigFiles"],
        )

    def test_selects_csharp_configuration_classes_unconditionally(self):
        # No registration-style content and no config-filename match -- only the class-name
        # convention itself is evidence, matching how repositories bind appsettings sections to
        # strongly-typed classes instead of (or in addition to) reading raw JSON.
        result = scan(SOURCE, archive({
            "src/Configuration/ApplicationConfiguration.cs": "public class ApplicationConfiguration { public string RedisConnectionString { get; set; } }",
            "src/Configuration/CacheOptions.cs": "public class CacheOptions { public string Host { get; set; } }",
            "src/Configuration/AutoMapperConfig.cs": "public class AutoMapperConfig { }",
        }))

        paths = result["localDevConfigFiles"]
        self.assertEqual(2, len(paths))
        self.assertTrue(any(path.endswith("ApplicationConfiguration.cs") for path in paths))
        self.assertTrue(any(path.endswith("CacheOptions.cs") for path in paths))
        self.assertFalse(any(path.endswith("AutoMapperConfig.cs") for path in paths))

    def test_registration_code_only_counts_when_no_compose_file_present(self):
        redis_content = 'services.AddStackExchangeRedisCache(options => { options.Configuration = "localhost:6379"; });'

        without_compose = scan(SOURCE, archive({
            "src/Startup.cs": redis_content,
        }))
        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Startup.cs"],
            without_compose["localDevConfigFiles"],
        )

        with_compose = scan(SOURCE, archive({
            "src/Startup.cs": redis_content,
            "src/deploy/docker-compose.yml": "version: '3'\nservices:\n  redis:\n    image: redis\n",
        }))
        paths = with_compose["localDevConfigFiles"]
        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/deploy/docker-compose.yml"],
            paths,
        )
        self.assertFalse(any(path.endswith("Startup.cs") for path in paths))

    def test_ignores_test_and_regression_directories(self):
        result = scan(SOURCE, archive({
            "src/Foo.RegressionTests/appsettings.json": '{"Logging":{"LogLevel":{"Default":"Information"}}}',
        }))

        self.assertEqual([], result["localDevConfigFiles"])
        self.assertEqual([], result["excludedFiles"])

    def test_never_selects_files_outside_the_requested_tree(self):
        result = scan(SOURCE, archive({
            "src/appsettings.json": '{"Logging":{"LogLevel":{"Default":"Information"}}}',
            "other/appsettings.json": '{"Logging":{"LogLevel":{"Default":"Information"}}}',
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/appsettings.json"],
            result["localDevConfigFiles"],
        )

    def test_reports_oversized_and_non_utf8_files_as_excluded(self):
        result = scan(SOURCE, archive({
            "src/appsettings.json": "x" * (512 * 1024 + 1),
            "src/settings.json": b"\xff\xfe\x00bad",
        }))

        self.assertEqual([], result["localDevConfigFiles"])
        self.assertEqual(
            [
                {"path": "src/appsettings.json", "reason": "file_too_large"},
                {"path": "src/settings.json", "reason": "not_utf8"},
            ],
            result["excludedFiles"],
        )


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual("https://api.github.com/repos/source/app/zipball/main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))

    def test_without_a_token_the_public_archive_is_downloaded(self):
        request = self.download(None)
        self.assertEqual("https://codeload.github.com/source/app/zip/main", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))
