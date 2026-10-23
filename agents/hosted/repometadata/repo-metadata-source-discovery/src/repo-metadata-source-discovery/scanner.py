"""Deterministically select .csproj and global.json files that evidence a repository's .NET versions."""

from __future__ import annotations

import io
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
IGNORED_PARTS = {".git", ".github", ".vs", ".vscode", "bin", "node_modules", "obj", "packages"}


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
    return bool({part.lower() for part in path.split("/")} & IGNORED_PARTS)


def _under_base(path: str, base: str) -> bool:
    return not base or path == base or path.startswith(f"{base}/")


def _basename(path: str) -> str:
    return path.lower().rsplit("/", 1)[-1]


def _is_candidate(filename: str) -> bool:
    return filename.endswith(".csproj") or filename == "global.json"


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
        # Authenticated GitHub requests get a 5,000/hour rate limit instead of the 60/hour anonymous
        # limit that a repeatedly-invoked discovery agent can exhaust quickly.
        headers = {"User-Agent": "repo-metadata-source-discovery"}
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
                if (
                    not separator
                    or not _under_base(path, location.base_path)
                    or _ignored(path)
                    or not _is_candidate(_basename(path))
                ):
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

    records.sort(key=lambda item: item[0].lower())
    files: list[str] = []
    total_bytes = 0
    for path, _content, size in records:
        if len(files) == MAX_FILES:
            excluded.append({"path": path, "reason": "file_limit"})
        elif total_bytes + size > MAX_TOTAL_BYTES:
            excluded.append({"path": path, "reason": "bundle_too_large"})
        else:
            files.append(_blob(location, path))
            total_bytes += size
    return {"repoMetadataFiles": files, "excludedFiles": sorted(excluded, key=lambda item: item["path"])}
