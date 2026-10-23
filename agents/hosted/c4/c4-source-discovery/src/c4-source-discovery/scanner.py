"""Deterministically select source files useful for C4 context and container diagrams."""

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
    ".bicep", ".config", ".cs", ".csproj", ".dockerfile", ".fs", ".go", ".gradle", ".java", ".js", ".json",
    ".jsx", ".kt", ".php", ".props", ".proto", ".py", ".rb", ".sln", ".tf", ".toml", ".ts", ".tsx",
    ".vb", ".xml", ".yaml", ".yml",
}
SUPPORTED_FILENAMES = {
    "dockerfile", "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml",
    "package.json", "pom.xml", "build.gradle", "settings.gradle", "requirements.txt", "pyproject.toml",
}
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
ENTRYPOINT_RE = re.compile(
    r"\b(?:WebApplication\.CreateBuilder|Host\.CreateDefaultBuilder|SpringApplication\.run|FastAPI\s*\(|"
    r"express\s*\(|createServer\s*\(|Flask\s*\(|Django|func\s+main\s*\(|public\s+static\s+void\s+main)\b",
    re.IGNORECASE,
)
CONTAINER_RE = re.compile(
    r"\b(?:Controller|Endpoint|Router|Handler|Service|Repository|DbContext|MessageHandler|Consumer|Producer|"
    r"AddHostedService|AddDbContext|AddSingleton|AddScoped|AddTransient|IHttpClientFactory|AddHttpClient|"
    r"Kafka|ServiceBus|RabbitMQ|SQS|SNS|Redis|Cosmos|SqlConnection|Npgsql|MongoClient|BlobServiceClient)\b",
    re.IGNORECASE,
)
INFRA_RE = re.compile(
    r"\b(?:container|image|service|resource|module|Microsoft\.|aws_|google_|azurerm_|kubernetes|helm|"
    r"database|queue|topic|storage|redis|cosmos|sql|postgres|mysql)\b",
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
    lowered = path.lower()
    filename = lowered.rsplit("/", 1)[-1]
    return filename in SUPPORTED_FILENAMES or lowered.endswith(tuple(SUPPORTED_EXTENSIONS))


def _candidate(path: str, content: str) -> bool:
    lowered = path.lower()
    filename = lowered.rsplit("/", 1)[-1]
    if filename in SUPPORTED_FILENAMES or filename in CONFIG_NAMES:
        return True
    if lowered.endswith((".bicep", ".tf", ".yaml", ".yml", ".json")) and INFRA_RE.search(content):
        return True
    return bool(ENTRYPOINT_RE.search(content) or CONTAINER_RE.search(content))


def _priority(path: str, content: str) -> tuple[int, str]:
    lowered = path.lower()
    filename = lowered.rsplit("/", 1)[-1]
    if ENTRYPOINT_RE.search(content) or filename in {"dockerfile", "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}:
        return (0, lowered)
    if filename in CONFIG_NAMES or filename.endswith((".csproj", ".sln", "package.json", "pyproject.toml")):
        return (1, lowered)
    if lowered.endswith((".bicep", ".tf", ".yaml", ".yml")):
        return (2, lowered)
    if CONTAINER_RE.search(content):
        return (3, lowered)
    return (4, lowered)


def _blob(location: SourceLocation, path: str) -> str:
    return (
        f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/"
        f"{urllib.parse.quote(location.repository, safe='')}/blob/{urllib.parse.quote(location.ref, safe='')}/"
        f"{urllib.parse.quote(path, safe='/')}"
    )


def _download_archive(location: SourceLocation, tenant: str | None = None) -> bytes:
    owner, repository, ref = (urllib.parse.quote(value, safe="") for value in (location.owner, location.repository, location.ref))
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    headers = {"User-Agent": "c4-source-discovery"}
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
