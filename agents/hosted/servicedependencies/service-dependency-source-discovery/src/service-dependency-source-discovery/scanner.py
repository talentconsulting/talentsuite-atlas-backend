"""Deterministically select files that evidence outbound API dependencies."""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

import github_app


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_FILES = 150
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 3 * 1024 * 1024
SUPPORTED_EXTENSIONS = {
    ".bicep", ".config", ".cs", ".csproj", ".go", ".gradle", ".java", ".js", ".json", ".jsx", ".kt",
    ".php", ".props", ".proto", ".py", ".rb", ".tf", ".targets", ".toml", ".ts", ".tsx", ".xml", ".yaml", ".yml",
}
# Entry-point/project files are always worth scanning even without dependency-registration evidence --
# they're what reveals whether a project is a web app, an API, a job, or a message handler.
CONTAINER_SHAPE_FILENAMES = {"program.cs", "startup.cs"}
IGNORED_PARTS = {
    ".git", ".github", ".idea", ".vs", ".vscode", "bin", "build", "coverage", "dist", "fixtures",
    "mocks", "node_modules", "obj", "packages", "snapshots", "test", "tests", "testdata", "unittests",
    "integrationtests", "acceptancetests", "regressiontests", "regression", "testharness", "fakeservers",
}
# .NET test projects are conventionally folders named after their namespace (e.g.
# "SFA.DAS.CommitmentsV2.UnitTests"), so the whole path segment never equals a bare keyword like
# "unittests" above -- match on the dotted suffix instead.
IGNORED_SUFFIXES = (
    ".tests", ".unittests", ".integrationtests", ".acceptancetests", ".regressiontests",
    ".testharness", ".fakeservers",
)
CONFIG_NAMES = {
    "app.config", "appsettings.json", "appsettings.development.json", "application.json", "application.yml",
    "application.yaml", "config.json", "local.settings.json", "settings.json", "web.config",
}
API_INTEGRATION_RE = re.compile(
    r"\b(?:AddHttpClient|HttpClient|IHttpClientFactory|AddRefitClient|Refit|RestClient|WebClient|"
    r"axios\b|fetch\s*\(|requests\.|httpx\.|AddGrpcClient|GrpcChannel|grpc\.|ManagedChannel|"
    r"ChannelForAddress|BaseAddress|BaseUrl|BaseUri|ApiUrl|ApiEndpoint|Audience|Scope)\b",
    re.IGNORECASE,
)
PROTO_SERVICE_RE = re.compile(r"\b(?:service|rpc)\s+[A-Za-z_]\w*", re.IGNORECASE)
REGISTRATION_RE = re.compile(
    r"\b(?:AddHttpClient|AddRefitClient|AddGrpcClient|Register.*(?:Api)?Client)\b",
    re.IGNORECASE,
)
# A generic DI registration (AddTransient/AddScoped/AddSingleton<TInterface, TImplementation>) counts
# as a client registration only when both type names follow the "...Client" naming convention -- this
# catches bulk ServiceRegistrations-style files that wire up an API client without AddHttpClient (e.g.
# `AddTransient<IReservationsOuterApiClient, ReservationsOuterApiClient>()`) while still excluding the
# many unrelated domain-service registrations (`AddScoped<IProviderService, ProviderService>()`) that
# typically sit right next to them in the same file.
GENERIC_CLIENT_REGISTRATION_RE = re.compile(
    r"\bAdd(?:Transient|Scoped|Singleton)\s*<\s*I?\w*(?:Api)?Client\s*,\s*\w*(?:Api)?Client\s*>",
    re.IGNORECASE,
)
# Evidence that an HTTP/gRPC client is actually being built or configured, for codebases that wire
# clients by hand (a factory calling `new HttpClient()`, `IHttpClientFactory.CreateClient(...)`,
# explicit `.BaseAddress =`) instead of the standard DI extension methods above. Deliberately narrower
# than mere usage (e.g. a controller just calling a method on an already-injected client) -- this only
# matches the site where the client itself comes into existence.
CLIENT_CONSTRUCTION_RE = re.compile(
    r"\b(?:new\s+HttpClient\s*\(|CreateHttpClient\s*\(|IHttpClientFactory\b|\.BaseAddress\s*=|"
    r"new\s+GrpcChannel|ChannelForAddress\s*\(|new\s+RestClient\s*\(|axios\.create\s*\(|"
    r"requests\.Session\s*\(|httpx\.Client\s*\()",
    re.IGNORECASE,
)
# Evidence that a Redis cache client is actually being constructed or registered. Deliberately narrower
# than mere consumption of an injected IDistributedCache/ICache -- that interface doesn't say which
# technology backs it, so only the site where a Redis-specific client or option type is configured counts.
CACHE_REGISTRATION_RE = re.compile(
    r"\b(?:AddStackExchangeRedisCache|AddDistributedRedisCache|StackExchangeRedisCacheOptions|"
    r"ConnectionMultiplexer\.Connect(?:Async)?\s*\(|RedisCacheOptions|new\s+Redis(?:Client)?\s*\(|"
    r"redis\.createClient\s*\(|redis\.Redis\s*\(|redis\.StrictRedis\s*\()",
    re.IGNORECASE,
)
# Evidence that a database client, connection, or ORM context is used -- these are all
# technology-specific type names, so a bare reference is enough to evidence the dependency.
DATABASE_REGISTRATION_RE = re.compile(
    r"\b(?:AddDbContext(?:Pool)?|DbContext|SqlConnection|NpgsqlConnection|MongoClient|CosmosClient)\b",
    re.IGNORECASE,
)
# Evidence that a message-broker producer, consumer, or channel is used -- these are all
# technology-specific type names, unlike a generic IConsumer/IProducer interface, so a bare
# reference is enough (the same treatment DATABASE_REGISTRATION_RE gives DbContext).
MESSAGE_BROKER_REGISTRATION_RE = re.compile(
    r"\b(?:ServiceBusClient|ServiceBusProcessor|ServiceBusSender|new\s+ConnectionFactory\s*\(|"
    r"RabbitMQ\.Client|IModel\b|ProducerBuilder|ConsumerBuilder|KafkaProducer|KafkaConsumer|"
    r"AmazonSQSClient|AmazonSNSClient)\b",
    re.IGNORECASE,
)
# Evidence that an object-storage (blob/bucket) client is used -- technology-specific type names.
OBJECT_STORAGE_REGISTRATION_RE = re.compile(
    r"\b(?:BlobServiceClient|BlobContainerClient|AmazonS3Client)\b",
    re.IGNORECASE,
)
# Evidence that another cloud-service SDK client (secrets, key vault, tables, and similar) is used
# -- technology-specific type names.
CLOUD_SERVICE_REGISTRATION_RE = re.compile(
    r"\b(?:SecretClient|KeyVaultClient|TableServiceClient|AmazonDynamoDBClient)\b",
    re.IGNORECASE,
)


class ScanError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceLocation:
    owner: str
    repository: str
    ref: str
    base_path: str


def parse_source_url(value: str) -> SourceLocation:
    parsed = urllib.parse.urlparse(value)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) < 4
        or parts[2] != "tree"
        or any(part in {".", ".."} or "/" in part or "\\" in part for part in parts)
    ):
        raise ScanError("sourceUrl must match https://github.com/owner/repository/tree/ref[/path].")
    owner, repository, _, ref, *path = parts
    return SourceLocation(owner, repository.removesuffix(".git"), ref, "/".join(path))


def _ignored(path: str) -> bool:
    parts = {part.lower() for part in path.split("/")}
    return bool(parts & IGNORED_PARTS) or any(part.endswith(IGNORED_SUFFIXES) for part in parts)


def _under_base(path: str, base: str) -> bool:
    return not base or path == base or path.startswith(f"{base}/")


def _supported(path: str) -> bool:
    return path.lower().endswith(tuple(SUPPORTED_EXTENSIONS))


def _is_container_shape_evidence(filename: str) -> bool:
    # Deliberately filename-only, not content-based: a content marker like IHandleMessages or
    # BackgroundService appears on every one of a project's many handler/job classes, not just its
    # entry point, and including all of them would crowd out real dependency evidence in the bundle.
    return filename in CONTAINER_SHAPE_FILENAMES or filename.endswith(".csproj")


def _candidate(path: str, content: str) -> bool:
    lowered = path.lower()
    filename = lowered.rsplit("/", 1)[-1]
    is_registration = (
        bool(REGISTRATION_RE.search(content))
        or bool(CLIENT_CONSTRUCTION_RE.search(content))
        or bool(GENERIC_CLIENT_REGISTRATION_RE.search(content))
        or bool(CACHE_REGISTRATION_RE.search(content))
        or bool(DATABASE_REGISTRATION_RE.search(content))
        or bool(MESSAGE_BROKER_REGISTRATION_RE.search(content))
        or bool(OBJECT_STORAGE_REGISTRATION_RE.search(content))
        or bool(CLOUD_SERVICE_REGISTRATION_RE.search(content))
    )
    is_proto_service = lowered.endswith(".proto") and bool(PROTO_SERVICE_RE.search(content))
    is_api_configuration = filename in CONFIG_NAMES and bool(API_INTEGRATION_RE.search(content))
    return is_registration or is_proto_service or is_api_configuration or _is_container_shape_evidence(filename)


def _priority(path: str, content: str) -> tuple[int, str]:
    lowered = path.lower()
    filename = lowered.rsplit("/", 1)[-1]
    if (
        REGISTRATION_RE.search(content)
        or CLIENT_CONSTRUCTION_RE.search(content)
        or GENERIC_CLIENT_REGISTRATION_RE.search(content)
        or CACHE_REGISTRATION_RE.search(content)
        or DATABASE_REGISTRATION_RE.search(content)
        or MESSAGE_BROKER_REGISTRATION_RE.search(content)
        or OBJECT_STORAGE_REGISTRATION_RE.search(content)
        or CLOUD_SERVICE_REGISTRATION_RE.search(content)
        or filename in CONFIG_NAMES
        or _is_container_shape_evidence(filename)
    ):
        return (0, lowered)
    if filename.rsplit(".", 1)[0].endswith(
        ("client", "connector", "gateway", "producer", "consumer", "repository", "dbcontext")
    ):
        return (1, lowered)
    return (2, lowered)


def _blob(location: SourceLocation, path: str) -> str:
    return (
        f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/"
        f"{urllib.parse.quote(location.repository, safe='')}/blob/{urllib.parse.quote(location.ref, safe='')}/"
        f"{urllib.parse.quote(path, safe='/')}"
    )


def _download_archive(location: SourceLocation, tenant: str | None = None) -> bytes:
    owner, repository, ref = (urllib.parse.quote(value, safe="") for value in (location.owner, location.repository, location.ref))
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    headers = {"User-Agent": "service-dependency-source-discovery"}
    token = github_app.read_token(tenant)
    if token:
        # The API's zipball endpoint accepts App installation tokens and redirects to a signed codeload URL.
        url = f"https://api.github.com/repos/{owner}/{repository}/zipball/{ref}"
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ScanError(f"GitHub returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise ScanError("Unable to download the GitHub repository archive.") from error
    if len(body) > MAX_ARCHIVE_BYTES:
        raise ScanError("GitHub archive exceeds the 100 MiB limit.")
    return body


def scan(source_url: str, archive_bytes: bytes | None = None, tenant: str | None = None) -> dict[str, object]:
    location = parse_source_url(source_url)
    selected: list[tuple[tuple[int, str], str, int]] = []
    excluded: list[dict[str, str]] = []
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes if archive_bytes is not None else _download_archive(location, tenant))) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                total_uncompressed += member.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise ScanError("GitHub archive exceeds the uncompressed size limit.")
                _, separator, path = member.filename.partition("/")
                if not separator or not _under_base(path, location.base_path) or _ignored(path) or not _supported(path):
                    continue
                if member.file_size > MAX_FILE_BYTES:
                    excluded.append({"path": path, "reason": "file_too_large"})
                    continue
                try:
                    content = archive.read(member).decode("utf-8-sig")
                except UnicodeDecodeError:
                    excluded.append({"path": path, "reason": "not_utf8"})
                    continue
                if _candidate(path, content):
                    selected.append((_priority(path, content), path, member.file_size))
    except zipfile.BadZipFile as error:
        raise ScanError("GitHub returned an invalid source archive.") from error
    selected.sort()
    files: list[str] = []
    total_bytes = 0
    for _, path, size in selected:
        if len(files) == MAX_FILES:
            excluded.append({"path": path, "reason": "file_limit"})
        elif total_bytes + size > MAX_TOTAL_BYTES:
            excluded.append({"path": path, "reason": "bundle_too_large"})
        else:
            files.append(_blob(location, path))
            total_bytes += size
    return {"sourceFiles": files, "excludedFiles": sorted(excluded, key=lambda item: item["path"])}
