"""Deterministically select source files that define events and commands."""

from __future__ import annotations

import io
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

import github_app


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
# Large repositories (e.g. das-commitments, ~460 message files) need more than the original 100 files.
MAX_FILES = int(os.getenv("EVENTCATALOG_DISCOVERY_MAX_FILES", "500"))
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = int(os.getenv("EVENTCATALOG_DISCOVERY_MAX_BYTES", str(10 * 1024 * 1024)))
SUPPORTED_EXTENSIONS = {".cs", ".go", ".java", ".js", ".jsx", ".kt", ".php", ".prisma", ".py", ".rb", ".sql", ".ts", ".tsx", ".xml", ".yaml", ".yml"}
IGNORED_PARTS = {".git", ".github", ".idea", ".vs", ".vscode", "adhocscripts", "bin", "build", "dist", "node_modules", "obj", "packages", "test", "tests", "unittests", "integrationtests", "acceptancetests", "testharness", "fakeservers"}
IGNORED_SUFFIXES = (".tests", ".unittests", ".integrationtests", ".acceptancetests", ".testharness", ".fakeservers")
MESSAGE_PATH_PARTS = {"command", "commands", "event", "events", "handler", "handlers", "message", "messages", "messaging"}
MESSAGE_MARKER_RE = re.compile(
    r"\b(?:IRequest(?:Handler)?\s*<|INotification(?:Handler)?\s*<|ICommand(?:Handler)?\b|"
    r"IEvent(?:Handler)?\b|CommandHandler\b|EventHandler\b|DomainEvent\b|IntegrationEvent\b|"
    r"record\s+\w+(?:Command|Event)\b|class\s+\w+(?:Command|Event|CommandHandler|EventHandler)\b|"
    r"struct\s+\w+(?:Command|Event)\b|implements\s+(?:Command|Event)\b|"
    r"@(?:CommandHandler|EventHandler|EventsHandler)\b|publish\s*\(|send\s*\(|emit\s*\()",
    re.IGNORECASE,
)


# A file that imports a ...Messages namespace and sends, publishes or routes a message: where a repository sends
# messages (its own or another service's, e.g. SFA.DAS.Notifications.Messages.Commands.SendEmailCommand).
MESSAGES_USING_RE = re.compile(r"^\s*using\s+[\w.]*\.Messages\b[\w.]*\s*;", re.MULTILINE)
SENDS_RE = re.compile(r"RouteToEndpoint\s*\(\s*typeof\s*\(|\.(?:Send|SendLocal|Publish)\s*(?:<\s*\w+\s*>\s*)?\(")


def _sends_messages(content: str) -> bool:
    return bool(MESSAGES_USING_RE.search(content) and SENDS_RE.search(content))


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


def _message_source(path: str, content: str) -> bool:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    named_message = stem.endswith(("command", "event", "commandhandler", "eventhandler"))
    return (named_message or bool({part.lower() for part in path.split("/")[:-1]} & MESSAGE_PATH_PARTS)
            or bool(MESSAGE_MARKER_RE.search(content)) or _sends_messages(content))


def _priority(path: str, content: str) -> tuple[int, str]:
    lowered = path.lower()
    if _sends_messages(content):   # ahead of everything, so the file limit never drops where messages are sent
        return (-1, lowered)
    if re.search(r"(?:Command|Event)(?:Handler)?\.", path, re.IGNORECASE) or "/commands/" in f"/{lowered}" or "/events/" in f"/{lowered}":
        return (0, lowered)
    if re.search(r"\b(?:IRequest|INotification|ICommand|IEvent)(?:Handler)?\b", content, re.IGNORECASE):
        return (1, lowered)
    return (2, lowered)


def _blob(location: SourceLocation, path: str) -> str:
    return f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/{urllib.parse.quote(location.repository, safe='')}/blob/{urllib.parse.quote(location.ref, safe='')}/{urllib.parse.quote(path, safe='/')}"


def _download_archive(location: SourceLocation, tenant: str | None = None) -> bytes:
    owner, repository, ref = (urllib.parse.quote(value, safe="") for value in (location.owner, location.repository, location.ref))
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    headers = {"User-Agent": "eventcatalog-source-discovery"}
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
                if _message_source(path, content):
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
