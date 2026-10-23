"""Deterministically select the source files needed to describe one database."""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

import github_app


# Authenticated GitHub requests get a 5,000/hour rate limit instead of the 60/hour anonymous
# limit that a repeatedly-invoked discovery agent can exhaust quickly. Reuses the same PAT the
# PR-creator agents already hold for writes.
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_FILES = 100
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".cs", ".go", ".java", ".js", ".jsx", ".kt", ".php", ".prisma", ".py", ".rb", ".sql", ".ts", ".tsx", ".xml", ".yaml", ".yml"}
IGNORED_PARTS = {".git", ".github", ".idea", ".vs", ".vscode", "adhocscripts", "bin", "build", "dist", "node_modules", "obj", "packages", "test", "tests", "unittests", "integrationtests", "acceptancetests", "regressiontests", "regression", "testharness", "fakeservers", "mock", "mocks", "stubs"}
# Mock/stub projects (e.g. SFA.DAS.ApprenticePortal.OuterApi.Mock) hold fake API models, not tables.
IGNORED_SUFFIXES = (".tests", ".unittests", ".integrationtests", ".acceptancetests", ".regressiontests", ".testharness", ".fakeservers", ".mock", ".mocks", ".mockapis", ".stubs", ".fakeapis")
DATABASE_PATH_PARTS = {"data", "database", "db", "entities", "entity", "migrations", "models", "persistence", "prisma", "schema", "schemas", "tables"}
# Stricter than DATABASE_MARKER_RE: evidence that something is actually persisted. A repository whose
# selected files contain none of these (e.g. an outer API whose "models" folders hold DTOs) has no
# database to describe, so discovery returns no files rather than letting the model guess.
PERSISTENCE_RE = re.compile(r"\[Table\s*\(|\b(?:DbContext|DbSet\s*<|IEntityTypeConfiguration\s*<|EntityTypeBuilder\s*<|ToTable\s*\(|CreateTable\s*\(|CREATE\s+TABLE\b|ALTER\s+TABLE\b|declarative_base\s*\(|mapped_column\s*\(|@Entity\b|@Table\b|sequelize\.define\s*\(|gorm:\"|ActiveRecord::Migration|create_table\s+|Schema::create\s+|Doctrine\\ORM|databaseChangeLog|<createTable\b)", re.IGNORECASE)
DATABASE_MARKER_RE = re.compile(r"\b(?:DbContext|DbSet\s*<|IEntityTypeConfiguration\s*<|EntityTypeBuilder\s*<|CreateTable\s*\(|CreateIndex\s*\(|CREATE\s+(?:TABLE|INDEX|TYPE)\b|ALTER\s+TABLE\b|FOREIGN\s+KEY\b|model\s+\w+\s*\{|enum\s+\w+\s*\{|declarative_base\s*\(|mapped_column\s*\(|relationship\s*\(|@Entity\b|@Table\b|sequelize\.define\s*\(|DataTypes\.|gorm:\"|ActiveRecord::Migration|create_table\s+|Schema::create\s+|Doctrine\\ORM|databaseChangeLog|<createTable\b|<entity\b)", re.IGNORECASE)


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
    if (parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or parsed.username or parsed.password or parsed.query or parsed.fragment or len(parts) < 4 or parts[2] != "tree" or any(part in {".", ".."} or "/" in part or "\\" in part for part in parts)):
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


def _database_source(path: str, content: str) -> bool:
    return bool({part.lower() for part in path.split("/")[:-1]} & DATABASE_PATH_PARTS) or bool(DATABASE_MARKER_RE.search(content))


def _persists(path: str, content: str) -> bool:
    return path.lower().endswith(".prisma") or "/migrations/" in f"/{path.lower()}" or bool(PERSISTENCE_RE.search(content))


def _priority(path: str, content: str) -> tuple[int, str]:
    lowered = path.lower()
    if "createtable(" in content.lower() or re.search(r"\bcreate\s+table\b", content, re.IGNORECASE) or "/migrations/" in f"/{lowered}":
        return (0, lowered)
    if "dbcontext" in content.lower() or "ientitytypeconfiguration" in content.lower() or "entitytypebuilder" in content.lower():
        return (1, lowered)
    return (2, lowered)


def _blob(location: SourceLocation, path: str) -> str:
    return f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/{urllib.parse.quote(location.repository, safe='')}/blob/{urllib.parse.quote(location.ref, safe='')}/{urllib.parse.quote(path, safe='/')}"


def _download_archive(location: SourceLocation, tenant: str | None = None) -> bytes:
    owner, repository, ref = (urllib.parse.quote(value, safe="") for value in (location.owner, location.repository, location.ref))
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    headers = {"User-Agent": "dbschema-source-discovery"}
    token = github_app.read_token(tenant)
    if token:
        # The API's zipball endpoint accepts App installation tokens and redirects to a signed codeload URL.
        url = f"https://api.github.com/repos/{owner}/{repository}/zipball/{ref}"
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
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
    has_persistence = False
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
                if _database_source(path, content):
                    selected.append((_priority(path, content), path, member.file_size))
                    has_persistence = has_persistence or _persists(path, content)
    except zipfile.BadZipFile as error:
        raise ScanError("GitHub returned an invalid source archive.") from error
    if not has_persistence:
        return {"schemaFiles": [], "excludedFiles": sorted(excluded, key=lambda item: item["path"])}
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
    return {"schemaFiles": files, "excludedFiles": sorted(excluded, key=lambda item: item["path"])}
