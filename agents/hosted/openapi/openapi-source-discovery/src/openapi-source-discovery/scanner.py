"""Deterministic GitHub API and payload-file discovery."""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass

import github_app



class ScanError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceLocation:
    owner: str
    repository: str
    ref: str
    base_path: str


IGNORED_PARTS = {
    ".git", ".github", ".vs", ".vscode", "bin", "build", "dist",
    "node_modules", "obj", "packages", "test", "tests", "unittests",
    "integrationtests", "acceptancetests", "testharness", "fakeservers",
    "testsubscriber", "testhelpers",
}
IGNORED_SUFFIXES = (
    ".tests", ".unittests", ".integrationtests", ".acceptancetests",
    ".testharness", ".fakeservers", ".testsubscriber", ".testhelpers",
)
MAX_SUPPORTING_FILES = 50
HTTP_ATTRIBUTE_RE = re.compile(
    r"\bHttp(?:Get|Post|Put|Patch|Delete|Head|Options)\b", re.IGNORECASE
)
MAPPED_ROUTE_RE = re.compile(
    r"\.Map(?:Get|Post|Put|Patch|Delete|Methods|Fallback)\s*\(", re.IGNORECASE
)
ACTION_SIGNATURE_RE = re.compile(
    r"(?P<attributes>(?:\s*\[[^\]]+\]\s*)+)"
    r"(?:public|protected|internal)\s+(?:async\s+)?"
    r"(?P<return_type>[\w<>,?\[\].\s]+?)\s+"
    r"(?P<name>\w+)\s*\((?P<parameters>[^)]*)\)",
    re.MULTILINE,
)
TYPE_DECLARATION_RE = re.compile(
    r"\b(?:class|struct|enum|interface|record(?:\s+(?:class|struct))?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\b"
)
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")
RETURN_EXPRESSION_RE = re.compile(r"\breturn\b(?P<expression>.*?);", re.DOTALL)
CONSTRUCTED_TYPE_RE = re.compile(r"\bnew\s+(?P<name>[A-Z][A-Za-z0-9_]*)\b")
COMMENT_OR_LITERAL_RE = re.compile(
    r"//[^\r\n]*|/\*.*?\*/|@\"(?:\"\"|[^\"])*\"|"
    r'\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\'',
    re.DOTALL,
)


def parse_source_url(source_url: str) -> SourceLocation:
    parsed = urllib.parse.urlparse(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ScanError("sourceUrl must be a credential-free HTTPS github.com URL.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "tree":
        raise ScanError(
            "sourceUrl must match "
            "https://github.com/<owner>/<repository>/tree/<ref>[/<path>]."
        )
    owner, repository, _, ref, *path_parts = parts
    if any(part in {".", ".."} for part in path_parts):
        raise ScanError("sourceUrl path contains an invalid traversal segment.")
    return SourceLocation(
        owner=owner,
        repository=repository.removesuffix(".git"),
        ref=ref,
        base_path="/".join(path_parts).strip("/"),
    )


def _is_ignored(path: str) -> bool:
    parts = {part.lower() for part in path.split("/")}
    return bool(parts & IGNORED_PARTS) or any(
        part.endswith(IGNORED_SUFFIXES) for part in parts
    )


def _is_under_base(path: str, base_path: str) -> bool:
    return not base_path or path == base_path or path.startswith(f"{base_path}/")


def _is_source_file(path: str) -> bool:
    return path.lower().endswith(".cs") and not _is_ignored(path)


def _is_api_file(path: str, content: str) -> bool:
    if not _is_source_file(path):
        return False
    lowered = path.lower()
    controller_candidate = (
        lowered.endswith("controller.cs") or "/controllers/" in lowered
    )
    return (
        controller_candidate and bool(HTTP_ATTRIBUTE_RE.search(content))
    ) or bool(MAPPED_ROUTE_RE.search(content))


def _code_only(content: str) -> str:
    return COMMENT_OR_LITERAL_RE.sub(lambda match: " " * len(match.group()), content)


def _download_sources(location: SourceLocation, tenant: str | None = None) -> dict[str, str]:
    owner = urllib.parse.quote(location.owner, safe="")
    repository = urllib.parse.quote(location.repository, safe="")
    ref = urllib.parse.quote(location.ref, safe="")
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    headers = {"User-Agent": "openapi-source-discovery"}
    token = github_app.read_token(tenant)
    if token:
        # The API's zipball endpoint accepts App installation tokens and redirects to a signed codeload URL.
        url = f"https://api.github.com/repos/{owner}/{repository}/zipball/{ref}"
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
            if content_length > 100 * 1024 * 1024:
                raise ScanError("GitHub archive exceeds the 100 MiB limit.")
            archive_bytes = response.read(100 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        raise ScanError(f"GitHub returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise ScanError("Unable to download the GitHub repository archive.") from error
    if len(archive_bytes) > 100 * 1024 * 1024:
        raise ScanError("GitHub archive exceeds the 100 MiB limit.")

    sources: dict[str, str] = {}
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                total_uncompressed += member.file_size
                if total_uncompressed > 250 * 1024 * 1024:
                    raise ScanError("GitHub archive exceeds the uncompressed size limit.")
                _, separator, relative_path = member.filename.partition("/")
                if separator and _is_source_file(relative_path):
                    sources[relative_path] = archive.read(member).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as error:
        raise ScanError("GitHub returned an invalid source archive.") from error
    return dict(sorted(sources.items()))


def _action_type_names(content: str) -> set[str]:
    names: set[str] = set()
    code = _code_only(content)
    for match in ACTION_SIGNATURE_RE.finditer(code):
        if not HTTP_ATTRIBUTE_RE.search(match.group("attributes")):
            continue
        signature = " ".join(
            (
                match.group("attributes"),
                match.group("return_type"),
                match.group("parameters"),
            )
        )
        names.update(re.findall(r"\b[A-Za-z_]\w*\b", signature))
        for expression in _returned_expressions(code, match.end()):
            names.update(
                constructed.group("name")
                for constructed in CONSTRUCTED_TYPE_RE.finditer(expression)
            )

    # Minimal API declarations often keep handler types on the same statement.
    for line in content.splitlines():
        if MAPPED_ROUTE_RE.search(line):
            names.update(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", line))
    return names


def _returned_expressions(code: str, signature_end: int) -> list[str]:
    position = signature_end
    while position < len(code) and code[position].isspace():
        position += 1

    if code.startswith("=>", position):
        expression_end = code.find(";", position + 2)
        if expression_end >= 0:
            return [code[position + 2 : expression_end]]
        return []

    if position >= len(code) or code[position] != "{":
        return []

    depth = 0
    for index in range(position, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                body = code[position + 1 : index]
                return [
                    match.group("expression")
                    for match in RETURN_EXPRESSION_RE.finditer(body)
                ]
    return []


def _build_type_index(
    sources: dict[str, str],
) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    declarations: dict[str, list[str]] = {}
    declarations_by_path: dict[str, set[str]] = {}
    for path, source in sources.items():
        for match in TYPE_DECLARATION_RE.finditer(source):
            name = match.group("name")
            declarations.setdefault(name, []).append(path)
            declarations_by_path.setdefault(path, set()).add(name)

    references = {}
    for path, source in sources.items():
        code = _code_only(source)
        references[path] = (
            set(IDENTIFIER_RE.findall(code)).intersection(declarations)
            - declarations_by_path.get(path, set())
        )
    return declarations, references


def _supporting_paths(
    api_path: str,
    content: str,
    declarations: dict[str, list[str]],
    references: dict[str, set[str]],
) -> list[str]:
    pending = deque(sorted(_action_type_names(content)))
    visited_types: set[str] = set()
    visited_paths: set[str] = set()
    supporting_paths: list[str] = []
    while pending and len(supporting_paths) < MAX_SUPPORTING_FILES:
        type_name = pending.popleft()
        if type_name in visited_types:
            continue
        visited_types.add(type_name)
        for path in sorted(declarations.get(type_name, [])):
            if path == api_path or path in visited_paths:
                continue
            visited_paths.add(path)
            supporting_paths.append(path)
            pending.extend(sorted(references[path] - visited_types))
            if len(supporting_paths) == MAX_SUPPORTING_FILES:
                break
    return supporting_paths


def _blob_url(location: SourceLocation, path: str) -> str:
    return (
        f"https://github.com/{urllib.parse.quote(location.owner, safe='')}/"
        f"{urllib.parse.quote(location.repository, safe='')}/blob/"
        f"{urllib.parse.quote(location.ref, safe='')}/"
        f"{urllib.parse.quote(path, safe='/')}"
    )


def scan(source_url: str, sources: dict[str, str] | None = None, tenant: str | None = None) -> list[dict[str, object]]:
    location = parse_source_url(source_url)
    repository_sources = sources if sources is not None else _download_sources(location, tenant)
    api_sources = [
        (path, content)
        for path, content in sorted(repository_sources.items())
        if _is_under_base(path, location.base_path) and _is_api_file(path, content)
    ]
    if not api_sources:
        return []

    declarations, references = _build_type_index(repository_sources)
    results: list[dict[str, object]] = []
    for path, content in api_sources:
        results.append(
            {
                "apiFile": _blob_url(location, path),
                "supportingFiles": [
                    _blob_url(location, supporting_path)
                    for supporting_path in _supporting_paths(
                        path, content, declarations, references
                    )
                ],
            }
        )
    return results
