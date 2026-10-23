import io
import unittest
import zipfile
from unittest import mock

import scanner
from scanner import ScanError, parse_source_url, scan


SOURCE_URL = "https://github.com/example/catalog/tree/feature-api/src/Api"


class SourceUrlTests(unittest.TestCase):
    def test_parses_full_repository_ref_and_path(self):
        location = parse_source_url(SOURCE_URL)
        self.assertEqual(location.owner, "example")
        self.assertEqual(location.repository, "catalog")
        self.assertEqual(location.ref, "feature-api")
        self.assertEqual(location.base_path, "src/Api")

    def test_rejects_non_tree_url(self):
        with self.assertRaises(ScanError):
            parse_source_url("https://github.com/example/catalog")


class ScanTests(unittest.TestCase):
    def test_returns_api_and_transitive_payload_files_as_blob_urls(self):
        sources = {
            "src/Api/Controllers/BidsController.cs": """
                [ApiController]
                public class BidsController : ControllerBase
                {
                    [HttpPost]
                    [ProducesResponseType(typeof(BidResponse), 201)]
                    public ActionResult<BidResponse> Create(
                        [FromBody] CreateBidRequest request) => Ok();
                }
            """,
            "src/Contracts/CreateBidRequest.cs": (
                "public record CreateBidRequest(Money Budget);"
            ),
            "src/Contracts/BidResponse.cs": (
                "public sealed record BidResponse(Guid Id);"
            ),
            "src/Contracts/Money.cs": "public record Money(decimal Amount);",
            "src/Services/BidService.cs": "public class BidService {}",
        }

        actual = scan(SOURCE_URL, sources=sources)

        self.assertEqual(
            actual,
            [
                {
                    "apiFile": (
                        "https://github.com/example/catalog/blob/feature-api/"
                        "src/Api/Controllers/BidsController.cs"
                    ),
                    "supportingFiles": [
                        "https://github.com/example/catalog/blob/feature-api/"
                        "src/Contracts/BidResponse.cs",
                        "https://github.com/example/catalog/blob/feature-api/"
                        "src/Contracts/CreateBidRequest.cs",
                        "https://github.com/example/catalog/blob/feature-api/"
                        "src/Contracts/Money.cs",
                    ],
                }
            ],
        )

    def test_does_not_return_api_files_outside_the_supplied_path(self):
        sources = {
            "src/Api/OrdersController.cs": (
                "[HttpGet] public class OrdersController {}"
            ),
            "src/Other/AdminController.cs": (
                "[HttpGet] public class AdminController {}"
            ),
        }

        actual = scan(SOURCE_URL, sources=sources)

        self.assertEqual(len(actual), 1)
        self.assertTrue(actual[0]["apiFile"].endswith("src/Api/OrdersController.cs"))

    def test_builds_one_type_index_for_all_api_files(self):
        sources = {
            "src/Api/OrdersController.cs": (
                "[HttpPost] public ActionResult<SharedRequest> Create(SharedRequest request) => Ok();"
            ),
            "src/Api/UsersController.cs": (
                "[HttpPut] public ActionResult<SharedRequest> Update(SharedRequest request) => Ok();"
            ),
            "src/Contracts/SharedRequest.cs": (
                "public record SharedRequest(Money Amount);"
            ),
            "src/Contracts/Money.cs": "public record Money(decimal Value);",
        }

        with mock.patch.object(
            scanner, "_build_type_index", wraps=scanner._build_type_index
        ) as build_type_index:
            actual = scan(SOURCE_URL, sources=sources)

        build_type_index.assert_called_once_with(sources)
        self.assertEqual(len(actual), 2)
        for item in actual:
            self.assertEqual(
                [
                    "https://github.com/example/catalog/blob/feature-api/"
                    "src/Contracts/SharedRequest.cs",
                    "https://github.com/example/catalog/blob/feature-api/"
                    "src/Contracts/Money.cs",
                ],
                item["supportingFiles"],
            )

    def test_prioritises_nearest_types_and_caps_supporting_files(self):
        sources = {
            "src/Api/OrdersController.cs": (
                "[HttpPost] public ActionResult<RootRequest> Create(RootRequest request) => Ok();"
            ),
            "src/Contracts/RootRequest.cs": "public record RootRequest(LevelOne Value);",
            "src/Contracts/LevelOne.cs": "public record LevelOne(LevelTwo Value);",
            "src/Contracts/LevelTwo.cs": (
                "public record LevelTwo(" + ", ".join(
                    f"Leaf{i} Value{i}" for i in range(60)
                ) + ");"
            ),
            **{
                f"src/Contracts/Leaf{i}.cs": f"public record Leaf{i}(string Value);"
                for i in range(60)
            },
        }

        supporting = scan(SOURCE_URL, sources=sources)[0]["supportingFiles"]

        self.assertEqual(len(supporting), scanner.MAX_SUPPORTING_FILES)
        self.assertTrue(supporting[0].endswith("/RootRequest.cs"))
        self.assertTrue(supporting[1].endswith("/LevelOne.cs"))
        self.assertTrue(supporting[2].endswith("/LevelTwo.cs"))

    def test_comments_and_literals_do_not_create_type_dependencies(self):
        sources = {
            "src/Api/OrdersController.cs": (
                "[HttpPost] public ActionResult<OrderRequest> Create(OrderRequest request) => Ok();"
            ),
            "src/Contracts/OrderRequest.cs": (
                "// UnrelatedType\n"
                "public record OrderRequest(string Value = \"UnrelatedType\");"
            ),
            "src/Services/UnrelatedType.cs": "public class UnrelatedType {}",
        }

        supporting = scan(SOURCE_URL, sources=sources)[0]["supportingFiles"]

        self.assertEqual(1, len(supporting))
        self.assertTrue(supporting[0].endswith("/OrderRequest.cs"))

    def test_ignores_test_project_sources(self):
        sources = {
            "src/Api/OrdersController.cs": (
                "[HttpPost] public ActionResult<OrderRequest> Create(OrderRequest request) => Ok();"
            ),
            "src/Contracts/OrderRequest.cs": "public record OrderRequest(string Value);",
            "src/Catalog.UnitTests/FakeRequest.cs": "public record FakeRequest(string Value);",
        }

        supporting = scan(SOURCE_URL, sources=sources)[0]["supportingFiles"]

        self.assertEqual(1, len(supporting))

    def test_discovers_concrete_response_constructed_inside_action(self):
        sources = {
            "src/Api/LearnerController.cs": """
                [HttpGet]
                public async Task<IActionResult> GetAllLearners()
                {
                    var query = new GetAllLearnersQuery();
                    return Ok(new GetAllLearnersResponse
                    {
                        Learners = result.Learners
                    });
                }
            """,
            "src/Contracts/GetAllLearnersResponse.cs": """
                public class GetAllLearnersResponse
                {
                    public List<Learner> Learners { get; set; }
                }
                public class Learner
                {
                    public string FirstName { get; set; }
                }
            """,
            "src/Models/Learner.cs": "public class Learner { public long Id { get; set; } }",
            "src/Application/GetAllLearnersQuery.cs": (
                "public class GetAllLearnersQuery {}"
            ),
        }

        supporting = scan(SOURCE_URL, sources=sources)[0]["supportingFiles"]

        self.assertEqual(
            [
                "https://github.com/example/catalog/blob/feature-api/"
                "src/Contracts/GetAllLearnersResponse.cs"
            ],
            supporting,
        )

    def test_discovers_concrete_response_from_expression_bodied_action(self):
        sources = {
            "src/Api/StatusController.cs": (
                "[HttpGet] public IActionResult Get() => Ok(new StatusResponse());"
            ),
            "src/Contracts/StatusResponse.cs": "public record StatusResponse(string Status);",
        }

        supporting = scan(SOURCE_URL, sources=sources)[0]["supportingFiles"]

        self.assertEqual(1, len(supporting))
        self.assertTrue(supporting[0].endswith("/StatusResponse.cs"))


class ArchiveDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w"):
            pass

        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return archive.getvalue()

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(scanner.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(scanner.urllib.request, "urlopen", side_effect=urlopen):
            scanner._download_sources(parse_source_url(SOURCE_URL), "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_downloads_through_the_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/example/catalog/zipball/feature-api", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))

    def test_without_a_token_the_public_archive_is_downloaded(self):
        request = self.download(None)
        self.assertEqual("https://codeload.github.com/example/catalog/zip/feature-api", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
