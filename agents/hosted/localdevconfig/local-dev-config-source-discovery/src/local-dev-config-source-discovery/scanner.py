"""Deterministically select files that evidence the local services and configuration a repository needs."""

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
MAX_FILES = 100
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
SUPPORTED_EXTENSIONS = {
    ".json", ".yml", ".yaml", ".xml", ".config", ".toml", ".ini", ".env",
    ".cs", ".csproj", ".props", ".targets", ".py", ".js", ".ts", ".ps1", ".sh",
}
IGNORED_PARTS = {
    ".git", ".github", ".idea", ".vs", ".vscode", "bin", "build", "coverage", "dist", "fixtures",
    "mocks", "node_modules", "obj", "packages", "snapshots", "test", "tests", "testdata", "unittests",
    "integrationtests", "acceptancetests", "regressiontests",
}
# .NET test projects are conventionally folders named after their namespace (e.g.
# "SFA.DAS.CommitmentsV2.UnitTests"), so the whole path segment never equals a bare keyword like
# "unittests" above -- match on the dotted suffix instead.
IGNORED_SUFFIXES = (".tests", ".unittests", ".integrationtests", ".acceptancetests", ".regressiontests")
CONFIG_NAMES = {
    "app.config", "appsettings.json", "appsettings.development.json", "appsettings.local.json",
    "application.json", "application.yml", "application.yaml", "config.json",
    "local.settings.json", "settings.json", "web.config",
}
DOCKER_COMPOSE_NAMES = {
    "docker-compose.yml", "docker-compose.yaml",
    "docker-compose.override.yml", "docker-compose.override.yaml",
    "compose.yml", "compose.yaml",
}
ENV_EXAMPLE_RE = re.compile(r"(?:^|\.)env\.(?:example|sample|template)$", re.IGNORECASE)
# A strongly-typed configuration class (e.g. ApplicationConfiguration.cs, SystemsConfiguration.cs,
# RedisSettings.cs, CacheOptions.cs) declares the configuration keys a repository binds from
# appsettings/environment variables -- often more reliably than appsettings.json itself, which may
# omit keys populated only via environment variables locally. Bare "*Config.cs" is deliberately
# excluded since that name is also conventionally used for unrelated DI/library setup classes
# (AutoMapperConfig.cs, SwaggerConfig.cs) that are not configuration-binding evidence.
CONFIG_CLASS_RE = re.compile(r"(?:configuration|settings|options)\.cs$", re.IGNORECASE)
# Evidence that a Redis cache client is actually being constructed or registered.
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
# reference is enough.
MESSAGE_BROKER_REGISTRATION_RE = re.compile(
    r"\b(?:ServiceBusClient|ServiceBusProcessor|ServiceBusSender|new\s+ConnectionFactory\s*\(|"
    r"RabbitMQ\.Client|IModel\b|ProducerBuilder|ConsumerBuilder|KafkaProducer|KafkaConsumer)\b",
    re.IGNORECASE,
)
# Evidence that an object-storage (blob/bucket) client is used -- technology-specific type names.
OBJECT_STORAGE_REGISTRATION_RE = re.compile(
    r"\b(?:BlobServiceClient|BlobContainerClient|AmazonS3Client)\b",
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


def _basename(path: str) -> str:
    return path.lower().rsplit("/", 1)[-1]


def _supported(path: str) -> bool:
    # An .env.example/.env.sample/.env.template file has no conventional extension of its own --
    # the whole trailing ".env.example" *is* the name -- so the plain suffix allowlist above is
    # widened to also admit anything ENV_EXAMPLE_RE recognises.
    return path.lower().endswith(tuple(SUPPORTED_EXTENSIONS)) or bool(ENV_EXAMPLE_RE.search(_basename(path)))


def _has_registration_evidence(content: str) -> bool:
    return bool(
        CACHE_REGISTRATION_RE.search(content)
        or DATABASE_REGISTRATION_RE.search(content)
        or MESSAGE_BROKER_REGISTRATION_RE.search(content)
        or OBJECT_STORAGE_REGISTRATION_RE.search(content)
    )


def _is_unconditional_candidate(filename: str) -> bool:
    return (
        filename in CONFIG_NAMES
        or filename in DOCKER_COMPOSE_NAMES
        or bool(ENV_EXAMPLE_RE.search(filename))
        or bool(CONFIG_CLASS_RE.search(filename))
    )


def _blob(location: SourceLocation, path: str) -> str:
    return (
        f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/"
        f"{urllib.parse.quote(location.repository, safe='')}/blob/{urllib.parse.quote(location.ref, safe='')}/"
        f"{urllib.parse.quote(path, safe='/')}"
    )


def _download_archive(location: SourceLocation, tenant: str | None = None) -> bytes:
    owner, repository, ref = (urllib.parse.quote(value, safe="") for value in (location.owner, location.repository, location.ref))
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    try:
        headers = {"User-Agent": "local-dev-config-source-discovery"}
        token = github_app.read_token(tenant)
        if token:
            # The API's zipball endpoint accepts App installation tokens and redirects to a signed codeload URL.
            url = f"https://api.github.com/repos/{owner}/{repository}/zipball/{ref}"
            headers["Authorization"] = f"Bearer {token}"
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
    excluded: list[dict[str, str]] = []
    records: list[tuple[str, str, int]] = []
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
                records.append((path, content, member.file_size))
    except zipfile.BadZipFile as error:
        raise ScanError("GitHub returned an invalid source archive.") from error

    # A docker-compose file is itself authoritative evidence of what local services a repository
    # needs, so once one is present in the tree, registration-code sniffing in application source
    # would only add noise (and risk crowding out the compose/config files with the same evidence).
    found_compose = any(_basename(path) in DOCKER_COMPOSE_NAMES for path, _, _ in records)

    selected: list[tuple[tuple[int, str], str, int]] = []
    for path, content, size in records:
        filename = _basename(path)
        if _is_unconditional_candidate(filename):
            selected.append(((0, path.lower()), path, size))
        elif not found_compose and _has_registration_evidence(content):
            selected.append(((1, path.lower()), path, size))

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
    return {"localDevConfigFiles": files, "excludedFiles": sorted(excluded, key=lambda item: item["path"])}
