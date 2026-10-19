"""Deterministically build the navigation graph of the web projects beneath one GitHub tree URL."""

from __future__ import annotations

import io
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

from navigation import extract, find_web_projects


# Authenticated GitHub requests get a 5,000/hour rate limit instead of the 60/hour anonymous limit.
GITHUB_TOKEN = os.getenv("GITHUB_READ_TOKEN")
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
# A describe batch is one model call: the files that define a handful of pages.
BATCH_MAX_FILES = int(os.getenv("USER_JOURNEYS_BATCH_MAX_FILES", "6"))
BATCH_MAX_BYTES = int(os.getenv("USER_JOURNEYS_BATCH_MAX_BYTES", str(160 * 1024)))
# Too many pages in one call and the model starts skipping transitions, so large controllers are split.
BATCH_MAX_PAGES = int(os.getenv("USER_JOURNEYS_BATCH_MAX_PAGES", "15"))
SOURCE_SUFFIXES = (".cs", ".cshtml", ".csproj")


class ScanError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
        raise ScanError("invalid_source_url", "sourceUrl must match https://github.com/owner/repository/tree/ref[/path].")
    owner, repository, _, ref, *path = parts
    return SourceLocation(owner, repository.removesuffix(".git"), ref, "/".join(path))


def _blob(location: SourceLocation, path: str) -> str:
    return (
        f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/"
        f"{urllib.parse.quote(location.repository, safe='')}/blob/{urllib.parse.quote(location.ref, safe='')}/"
        f"{urllib.parse.quote(path, safe='/')}"
    )


def _download_archive(location: SourceLocation) -> bytes:
    url = (
        f"https://codeload.github.com/{urllib.parse.quote(location.owner, safe='')}/"
        f"{urllib.parse.quote(location.repository, safe='')}/zip/{urllib.parse.quote(location.ref, safe='')}"
    )
    headers = {"User-Agent": "user-journeys-source-discovery"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
            body = response.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ScanError("source_unavailable", f"GitHub returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise ScanError("source_unavailable", "Unable to download the GitHub repository archive.") from error
    if len(body) > MAX_ARCHIVE_BYTES:
        raise ScanError("source_too_large", "GitHub archive exceeds the 100 MiB limit.")
    return body


def _read_sources(location: SourceLocation, archive_bytes: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    files: dict[str, str] = {}
    excluded: list[dict[str, str]] = []
    base = location.base_path
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                total += member.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ScanError("source_too_large", "GitHub archive exceeds the uncompressed size limit.")
                _, separator, path = member.filename.partition("/")
                if not separator or not path.endswith(SOURCE_SUFFIXES):
                    continue
                if base and not (path == base or path.startswith(base + "/")):
                    continue
                if member.file_size > MAX_FILE_BYTES:
                    excluded.append({"path": path, "reason": "file_too_large"})
                    continue
                try:
                    files[path] = archive.read(member).decode("utf-8-sig")
                except UnicodeDecodeError:
                    excluded.append({"path": path, "reason": "not_utf8"})
    except zipfile.BadZipFile as error:
        raise ScanError("source_unavailable", "GitHub returned an invalid source archive.") from error
    return files, excluded


def _batches(location: SourceLocation, graph: dict, files: dict[str, str]) -> list[dict]:
    """Group pages that share defining files (a controller, a page and its code-behind) into model-sized batches."""
    by_file: dict[tuple[str, ...], list[str]] = {}
    for page_id, paths in graph["pageFiles"].items():
        by_file.setdefault(tuple(paths), []).append(page_id)
    batches: list[dict] = []
    current = {"pageIds": [], "paths": []}
    size = 0
    for paths, page_ids in sorted(by_file.items()):
        new_paths = [p for p in paths if p not in current["paths"]]
        new_size = sum(len(files.get(p, "")) for p in new_paths)
        if current["pageIds"] and (len(current["paths"]) + len(new_paths) > BATCH_MAX_FILES or size + new_size > BATCH_MAX_BYTES):
            batches.append(current)
            current, size = {"pageIds": [], "paths": []}, 0
            new_paths, new_size = list(paths), sum(len(files.get(p, "")) for p in paths)
        current["pageIds"] += sorted(page_ids)
        current["paths"] += new_paths
        size += new_size
    if current["pageIds"]:
        batches.append(current)
    return [
        {"pageIds": b["pageIds"][i:i + BATCH_MAX_PAGES], "files": [_blob(location, p) for p in b["paths"]]}
        for b in batches for i in range(0, len(b["pageIds"]), BATCH_MAX_PAGES)
    ]


def scan(source_url: str, archive_bytes: bytes | None = None) -> dict[str, object]:
    location = parse_source_url(source_url)
    files, excluded = _read_sources(location, archive_bytes if archive_bytes is not None else _download_archive(location))
    projects = find_web_projects(list(files))
    graph = {"pages": [], "transitions": [], "siteNavigation": [], "unresolved": [], "selfLinkCount": 0, "pageFiles": {}}
    multiple = len(projects) > 1
    for project in projects:
        part = extract(files, project)
        # With several web projects in one repo, page ids are prefixed so they stay unique.
        prefix = (project.rsplit("/", 1)[-1] + ":") if multiple else ""
        rename = lambda page_id: prefix + page_id
        for page in part["pages"]:
            graph["pages"].append({**page, "id": rename(page["id"])})
        for t in part["transitions"]:
            graph["transitions"].append({**t, "from": rename(t["from"]), "to": rename(t["to"])})
        for link in part["siteNavigation"]:
            graph["siteNavigation"].append({**link, "to": rename(link["to"])})
        for u in part["unresolved"]:
            graph["unresolved"].append({**u, "from": rename(u["from"])})
        graph["selfLinkCount"] += part["selfLinkCount"]
        graph["pageFiles"].update({rename(k): v for k, v in part["pageFiles"].items()})
    batches = _batches(location, graph, files)
    del graph["pageFiles"]
    return {
        "repository": f"{location.owner}/{location.repository}",
        "ref": location.ref,
        "path": location.base_path,
        "webProjects": projects,
        "graph": graph,
        "batches": batches,
        "excludedFiles": sorted(excluded, key=lambda item: item["path"]),
    }
