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
    def test_selects_registration_and_configuration_files_only(self):
        result = scan(SOURCE, archive({
            "src/Startup.cs": 'services.AddHttpClient<IAccountsClient>(c => c.BaseAddress = new Uri(config["AccountsApi:BaseUrl"]));',
            "src/Clients/AccountsClient.cs": "class AccountsClient { private readonly HttpClient client; public AccountsClient(HttpClient client) { this.client = client; } }",
            "src/appsettings.json": '{"AccountsApi":{"BaseUrl":"https://example.invalid"}}',
            "src/Messaging/OrderConsumer.cs": "class OrderConsumer { ServiceBusClient bus; }",
            "src/Data/ProviderCommitmentsDbContext.cs": "class ProviderCommitmentsDbContext : DbContext { }",
            "src/Domain/Order.cs": "class Order { public long Id { get; set; } }",
        }))

        paths = result["sourceFiles"]
        self.assertEqual(4, len(paths))
        self.assertTrue(any(path.endswith("src/Startup.cs") for path in paths))
        self.assertTrue(any(path.endswith("src/appsettings.json") for path in paths))
        self.assertFalse(any(path.endswith("AccountsClient.cs") for path in paths))
        self.assertTrue(any(path.endswith("src/Messaging/OrderConsumer.cs") for path in paths))
        self.assertTrue(any(path.endswith("ProviderCommitmentsDbContext.cs") for path in paths))
        self.assertFalse(any(path.endswith("src/Domain/Order.cs") for path in paths))

    def test_a_dbcontext_declaration_is_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Data/ProviderCommitmentsDbContext.cs": "class ProviderCommitmentsDbContext : DbContext { }",
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Data/ProviderCommitmentsDbContext.cs"],
            result["sourceFiles"],
        )

    def test_a_servicebus_client_construction_is_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Messaging/OrderConsumer.cs": "class OrderConsumer { ServiceBusClient bus = new ServiceBusClient(config); }",
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Messaging/OrderConsumer.cs"],
            result["sourceFiles"],
        )

    def test_a_blob_storage_client_construction_is_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Storage/DocumentStore.cs": "class DocumentStore { BlobServiceClient client = new BlobServiceClient(config); }",
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Storage/DocumentStore.cs"],
            result["sourceFiles"],
        )

    def test_a_secret_client_construction_is_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Secrets/SecretsProvider.cs": "class SecretsProvider { SecretClient client = new SecretClient(vaultUri, credential); }",
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Secrets/SecretsProvider.cs"],
            result["sourceFiles"],
        )

    def test_a_client_that_only_consumes_an_injected_httpclient_is_not_a_registration(self):
        result = scan(SOURCE, archive({
            "src/Clients/AccountsClient.cs": (
                "class AccountsClient : IAccountsClient { "
                "private readonly HttpClient client; "
                "public AccountsClient(HttpClient client) { this.client = client; } "
                "public Task<Account> Get(string id) => client.GetFromJsonAsync<Account>($\"accounts/{id}\"); }"
            ),
        }))

        self.assertEqual([], result["sourceFiles"])

    def test_a_generic_di_registration_of_a_named_client_counts_as_a_registration(self):
        result = scan(SOURCE, archive({
            "src/AppStart/ServiceRegistrationExtension.cs": (
                "services.AddScoped<IProviderPermissionsService, ProviderPermissionsService>(); "
                "services.AddSingleton<IProviderService, ProviderService>(); "
                "services.AddTransient<HttpClient>(); "
                "services.AddTransient<IReservationsOuterApiClient, ReservationsOuterApiClient>(); "
                "services.AddTransient<ICacheStorageService, CacheStorageService>();"
            ),
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/AppStart/ServiceRegistrationExtension.cs"],
            result["sourceFiles"],
        )

    def test_a_file_registering_only_domain_services_is_not_selected(self):
        result = scan(SOURCE, archive({
            "src/AppStart/ServiceRegistrationExtension.cs": (
                "services.AddScoped<IProviderPermissionsService, ProviderPermissionsService>(); "
                "services.AddSingleton<IProviderService, ProviderService>(); "
                "services.AddTransient<ICacheStorageService, CacheStorageService>();"
            ),
        }))

        self.assertEqual([], result["sourceFiles"])

    def test_a_hand_rolled_client_factory_is_a_registration_even_without_addhttpclient(self):
        result = scan(SOURCE, archive({
            "src/DependencyResolution/ServiceRegistrationExtensions.cs": (
                "services.AddSingleton<IReservationsApiClient>(s => s.GetService<IReservationsApiClientFactory>().CreateClient());"
            ),
            "src/ReservationsApiClientFactory.cs": (
                "public class ReservationsApiClientFactory : IReservationsApiClientFactory { "
                "public IReservationsApiClient CreateClient() { "
                "var httpClient = CreateHttpClient(); "
                "return new ReservationsApiClient(httpClient); } "
                "private HttpClient CreateHttpClient() { "
                "var client = new HttpClient(); client.BaseAddress = new Uri(_configuration.ApiBaseUrl); return client; } }"
            ),
        }))

        paths = result["sourceFiles"]
        self.assertTrue(any(path.endswith("ReservationsApiClientFactory.cs") for path in paths))

    def test_a_redis_cache_registration_is_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Startup.cs": (
                'services.AddStackExchangeRedisCache(options => '
                '{ options.Configuration = config["Redis:ConnectionString"]; });'
            ),
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Startup.cs"],
            result["sourceFiles"],
        )

    def test_merely_consuming_an_injected_distributed_cache_is_not_a_registration(self):
        result = scan(SOURCE, archive({
            "src/Services/AccountLookupService.cs": (
                "class AccountLookupService { private readonly IDistributedCache cache; "
                "public AccountLookupService(IDistributedCache cache) { this.cache = cache; } "
                "public Task<string> Get(string id) => cache.GetStringAsync(id); }"
            ),
        }))

        self.assertEqual([], result["sourceFiles"])

    def test_ignores_dotted_dotnet_test_project_folders(self):
        result = scan(SOURCE, archive({
            "src/CommitmentsV2/SFA.DAS.CommitmentsV2.UnitTests/Infrastructure/Api/WhenCallingPost.cs": (
                'services.AddHttpClient<IApprovalsOuterApiClient, ApprovalsOuterApiClient>('
                'c => c.BaseAddress = new Uri("https://approvals.example.invalid"));'
            ),
        }))

        self.assertEqual([], result["sourceFiles"])

    def test_ignores_tests_and_files_outside_requested_tree(self):
        result = scan(SOURCE, archive({
            "src/Clients/BillingClient.cs": (
                'services.AddHttpClient<IBillingClient, BillingClient>('
                'c => c.BaseAddress = new Uri("https://billing.example.invalid"));'
            ),
            "src/Tests/FakeBillingClient.cs": 'services.AddHttpClient<IBillingClient, FakeBillingClient>();',
            "other/Clients/OutsideClient.cs": 'services.AddHttpClient<IOutsideClient, OutsideClient>();',
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Clients/BillingClient.cs"],
            result["sourceFiles"],
        )

    def test_a_csproj_file_is_always_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Api/SFA.DAS.CommitmentsV2.Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>',
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Api/SFA.DAS.CommitmentsV2.Api.csproj"],
            result["sourceFiles"],
        )

    def test_program_cs_is_always_a_candidate(self):
        result = scan(SOURCE, archive({
            "src/Api/Program.cs": "var builder = WebApplication.CreateBuilder(args);",
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Api/Program.cs"],
            result["sourceFiles"],
        )

    def test_container_shape_markers_do_not_select_every_matching_file(self):
        # A marker like BackgroundService or IHandleMessages appears on every one of a project's many
        # job/handler classes, not just its entry point. Only Program.cs/Startup.cs/*.csproj should be
        # selected for this signal -- selecting every matching file would crowd out real dependency
        # evidence in a repository with dozens of message handlers.
        result = scan(SOURCE, archive({
            "src/Jobs/Worker.cs": "public class Worker : BackgroundService { }",
            "src/MessageHandlers/OrderHandler.cs": "public class OrderHandler : IHandleMessages<OrderCreated> { }",
        }))

        self.assertEqual([], result["sourceFiles"])

    def test_program_cs_is_a_candidate_regardless_of_its_shape_marker(self):
        result = scan(SOURCE, archive({
            "src/Jobs/Program.cs": "public class Worker : BackgroundService { }",
        }))

        self.assertEqual(
            ["https://github.com/source/app/blob/main/src/Jobs/Program.cs"],
            result["sourceFiles"],
        )

    def test_reports_oversized_candidate(self):
        result = scan(SOURCE, archive({
            "src/Clients/LargeClient.cs": "HttpClient " + "x" * (512 * 1024),
        }))

        self.assertEqual([], result["sourceFiles"])
        self.assertEqual([{"path": "src/Clients/LargeClient.cs", "reason": "file_too_large"}], result["excludedFiles"])


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


if __name__ == "__main__":
    unittest.main()
