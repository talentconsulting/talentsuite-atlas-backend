"""Reads a batch of files from one download of a GitHub repository archive.

Fetching each file through the contents API costs one call against the GitHub App installation's
hourly limit per file; a whole manifest run fetched thousands and ran out. The zipball endpoint costs
one call however many files are read from it (its redirect to codeload.github.com is not counted).

This module is copied unchanged into every agent that uses it; keep the copies identical.
"""

from __future__ import annotations

import io
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import github_app

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
RETRYABLE_STATUSES = {500, 502, 503, 504}
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


class ArchiveError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _download(owner: str, repository: str, ref: str, tenant: str | None, user_agent: str) -> bytes:
    owner, repository, ref = (urllib.parse.quote(value, safe="") for value in (owner, repository, ref))
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    headers = {"User-Agent": user_agent}
    token = github_app.read_token(tenant)
    if token:
        # The API's zipball endpoint accepts App installation tokens and redirects to a signed codeload URL.
        url = f"https://api.github.com/repos/{owner}/{repository}/zipball/{ref}"
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                declared_size = int(response.headers.get("Content-Length", "0") or "0")
                if declared_size > MAX_ARCHIVE_BYTES:
                    raise ArchiveError("repository_too_large", "GitHub archive exceeds 100 MiB.")
                archive_bytes = response.read(MAX_ARCHIVE_BYTES + 1)
            break
        except ArchiveError:
            raise
        except urllib.error.HTTPError as error:
            if attempt == MAX_ATTEMPTS or error.code not in RETRYABLE_STATUSES:
                raise ArchiveError("source_unavailable", f"GitHub returned HTTP {error.code} for the repository archive.") from error
        except urllib.error.URLError as error:
            if attempt == MAX_ATTEMPTS:
                raise ArchiveError("source_unavailable", "Unable to download the GitHub repository archive.") from error
        time.sleep(RETRY_DELAY_SECONDS * attempt)
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("repository_too_large", "GitHub archive exceeds 100 MiB.")
    return archive_bytes


def read_files(
    owner: str,
    repository: str,
    ref: str,
    paths: list[str],
    *,
    tenant: str | None,
    user_agent: str,
    max_file_bytes: int,
    archive_bytes: bytes | None = None,
) -> dict[str, bytes]:
    """Returns the raw bytes of each requested repository-relative path.

    Raises ArchiveError with code source_unavailable when the archive cannot be downloaded or a path
    is not in it, and source_too_large when a requested file exceeds max_file_bytes.
    """
    wanted = set(paths)
    if archive_bytes is None:
        archive_bytes = _download(owner, repository, ref, tenant, user_agent)
    files: dict[str, bytes] = {}
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                total_uncompressed += member.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise ArchiveError("repository_too_large", "GitHub archive exceeds 250 MiB uncompressed.")
                # Members sit under one top-level folder named after the repository and commit.
                _, separator, path = member.filename.partition("/")
                if not separator or path not in wanted:
                    continue
                if member.file_size > max_file_bytes:
                    raise ArchiveError("source_too_large", f"Source file is too large: {path}")
                files[path] = archive.read(member)
    except zipfile.BadZipFile as error:
        raise ArchiveError("invalid_source", "GitHub returned an invalid repository archive.") from error
    missing = sorted(wanted - files.keys())
    if missing:
        raise ArchiveError("source_unavailable", f"{missing[0]} is not in the repository at {ref}.")
    return files
