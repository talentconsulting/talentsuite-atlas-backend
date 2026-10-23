"""Validate and publish database schemas through the GitHub API."""

from __future__ import annotations

import base64
import http.client
import json
import os
import posixpath
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


MAX_SCHEMAS = 100
MAX_TOTAL_BYTES = 10 * 1024 * 1024
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
# Creating a blob, tree, or commit is content-addressed and invisible until a ref points at it, so a
# repeat is harmless; creating a ref or pull request is not, and is never retried.
REPEATABLE_POST_SUFFIXES = ("/git/blobs", "/git/trees", "/git/commits")


class PublicationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _TransientGitHubError(Exception):
    def __init__(self, publication_error: PublicationError):
        super().__init__(str(publication_error))
        self.publication_error = publication_error


@dataclass(frozen=True)
class PlannedFile:
    path: str
    content: bytes
    action: str


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
    ):
        if not token:
            raise PublicationError("missing_github_token", "GITHUB_PR_TOKEN is not configured.")
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = retry_delay_seconds

    def request(self, method: str, path: str, payload: object | None = None) -> Any:
        repeatable = method == "GET" or (method == "POST" and path.endswith(REPEATABLE_POST_SUFFIXES))
        attempts = self.max_attempts if repeatable else 1
        for attempt in range(attempts):
            if attempt:
                time.sleep(self.retry_delay_seconds * 2 ** (attempt - 1))
            try:
                return self._request_once(method, path, payload)
            except _TransientGitHubError as error:
                if attempt == attempts - 1:
                    raise error.publication_error from error.__cause__
        raise AssertionError("unreachable")

    def _request_once(self, method: str, path: str, payload: object | None) -> Any:
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "dbschema-pr-creator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.api_url}{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_body = response.read()
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = str(json.loads(error.read().decode("utf-8")).get("message", ""))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
            message = f"GitHub API returned HTTP {error.code}."
            if detail:
                message += f" {detail}"
            if error.code in TRANSIENT_HTTP_STATUSES:
                raise _TransientGitHubError(PublicationError("github_api_error", message)) from error
            raise PublicationError("github_api_error", message) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            # urlopen raises bare TimeoutError / ConnectionResetError / RemoteDisconnected for read-phase
            # failures, not URLError, so they must be caught explicitly.
            raise _TransientGitHubError(
                PublicationError("github_unavailable", f"The GitHub API could not be reached ({type(error).__name__}).")
            ) from error
        return json.loads(response_body.decode("utf-8")) if response_body else {}


def parse_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise PublicationError("invalid_json", "Input must be one JSON object.") from error
    if not isinstance(payload, dict):
        raise PublicationError("invalid_input", "Input must be one JSON object.")
    validate_request(payload)
    return payload


def normalize_repository(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationError("invalid_repository", "repository must identify a GitHub repository.")
    repository = value.strip()
    if repository.startswith("https://github.com/"):
        parsed = urllib.parse.urlparse(repository)
        if parsed.query or parsed.fragment:
            raise PublicationError("invalid_repository", "repository must not contain a query or fragment.")
        repository = parsed.path.strip("/").removesuffix(".git")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise PublicationError("invalid_repository", "repository must use owner/repository format.")
    return repository


def _clean_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationError("invalid_input", f"{field} must be a non-empty relative path.")
    cleaned = posixpath.normpath(value.strip().strip("/"))
    if cleaned in {"", ".", ".."} or cleaned.startswith("../"):
        raise PublicationError("invalid_input", f"{field} must stay inside the repository.")
    return cleaned


def _source_repository(source_url: object) -> str:
    if not isinstance(source_url, str):
        raise PublicationError("invalid_schema", "sourceUrl must be a GitHub tree URL.")
    parsed = urllib.parse.urlparse(source_url)
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
    ):
        raise PublicationError("invalid_schema", "sourceUrl must be a GitHub tree URL.")
    return parts[1].removesuffix(".git")


def _validate_branch(value: str, field: str) -> str:
    if (
        not BRANCH_PATTERN.fullmatch(value)
        or value.startswith(("/", "."))
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "//" in value
    ):
        raise PublicationError("invalid_input", f"{field} is not a valid Git branch name.")
    return value


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "repository", "schemas", "targetDirectory", "baseBranch", "branchName",
        "pullRequestTitle", "pullRequestBody", "manifestFile", "manifestNodes",
    }
    if not set(payload).issubset(allowed):
        raise PublicationError("invalid_input", "Input contains unsupported properties.")
    repository = normalize_repository(payload.get("repository"))
    schemas = payload.get("schemas")
    if not isinstance(schemas, list) or not schemas:
        raise PublicationError("invalid_schemas", "schemas must be a non-empty array.")
    if len(schemas) > MAX_SCHEMAS:
        raise PublicationError("too_many_schemas", f"At most {MAX_SCHEMAS} schemas are allowed.")
    target_directory = _clean_relative_path(payload.get("targetDirectory", "db-schema"), "targetDirectory")
    validated = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for item in schemas:
        if not isinstance(item, dict) or set(item) not in (
            {"sourceUrl", "schema"}, {"sourceUrl", "schema", "repository"},
            {"sourceUrl", "schema", "targetPath"}, {"sourceUrl", "schema", "repository", "targetPath"},
        ):
            raise PublicationError("invalid_schema", "Each schema must contain sourceUrl and schema, with optional repository and targetPath.")
        schema = item["schema"]
        if not isinstance(schema, dict) or set(schema) != {"database", "tables", "types"}:
            raise PublicationError("invalid_schema", "Each schema must contain database, tables, and types.")
        if not isinstance(schema["database"], dict) or not isinstance(schema["tables"], list) or not isinstance(schema["types"], list):
            raise PublicationError("invalid_schema", "Each schema contains invalid database-schema fields.")
        output_path = _clean_relative_path(
            item.get("targetPath", posixpath.join(target_directory, _source_repository(item["sourceUrl"]), "database.schema.json")),
            "targetPath",
        )
        if not output_path.endswith(".json"):
            raise PublicationError("invalid_schema", "targetPath must end with .json.")
        if output_path in seen_paths:
            raise PublicationError("duplicate_output_path", f"Multiple schemas map to {output_path}.")
        seen_paths.add(output_path)
        content = (json.dumps(schema, indent=2) + "\n").encode("utf-8")
        total_bytes += len(content)
        validated.append({"sourceUrl": item["sourceUrl"], "path": output_path, "content": content})

    manifest_file = payload.get("manifestFile")
    if manifest_file is None and payload.get("manifestNodes") is not None:
        raise PublicationError("invalid_manifest_file", "manifestNodes requires manifestFile.")
    validated_manifest = None
    if manifest_file is not None:
        if not isinstance(manifest_file, dict) or set(manifest_file) != {"path", "content"}:
            raise PublicationError("invalid_manifest_file", "manifestFile must contain exactly path and content.")
        manifest_path = _clean_relative_path(manifest_file["path"], "manifestFile.path")
        if not manifest_path.endswith(".json"):
            raise PublicationError("invalid_manifest_file", "manifestFile.path must end with .json.")
        if manifest_path in seen_paths:
            raise PublicationError("duplicate_output_path", f"manifestFile collides with {manifest_path}.")
        try:
            manifest_content = (json.dumps(manifest_file["content"], indent=2) + "\n").encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PublicationError("invalid_manifest_file", "manifestFile.content must be JSON serializable.") from error
        total_bytes += len(manifest_content)
        validated_manifest = {
            "path": manifest_path, "content": manifest_content,
            "document": manifest_file["content"], "nodes": _manifest_nodes(payload.get("manifestNodes")),
        }
    if total_bytes > MAX_TOTAL_BYTES:
        raise PublicationError("schemas_too_large", "Generated database-schema files exceed 10 MiB.")

    result: dict[str, Any] = {
        "repository": repository,
        "schemas": validated,
        "targetDirectory": target_directory,
        "manifestFile": validated_manifest,
    }
    for field in ("baseBranch", "branchName", "pullRequestTitle", "pullRequestBody"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise PublicationError("invalid_input", f"{field} must be a non-empty string.")
            result[field] = _validate_branch(value.strip(), field) if field in {"baseBranch", "branchName"} else value.strip()
    return result


def default_branch_name() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"dbschema/{timestamp}-{uuid.uuid4().hex[:8]}"


def _encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _existing_content(client: GitHubClient, repository: str, path: str, branch: str) -> bytes | None:
    endpoint = f"/repos/{repository}/contents/{urllib.parse.quote(path, safe='/')}?ref={_encoded(branch)}"
    try:
        response = client.request("GET", endpoint)
    except PublicationError as error:
        if error.code == "github_api_error" and "HTTP 404" in str(error):
            return None
        raise
    if not isinstance(response, dict) or response.get("type") != "file":
        raise PublicationError("github_api_error", f"Destination path {path} is not a file.")
    try:
        return base64.b64decode(str(response["content"]), validate=False)
    except (KeyError, ValueError) as error:
        raise PublicationError("github_api_error", f"GitHub returned invalid content for {path}.") from error


def _manifest_nodes(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(node, str) or not node.strip() for node in value):
        raise PublicationError("invalid_manifest_file", "manifestNodes must be a non-empty array of manifest node names.")
    return [node.strip() for node in value]


def _merge_manifest(existing: bytes | None, manifest: dict[str, Any]) -> bytes:
    """Apply only this flow's manifest nodes to the manifest as it is now on the base branch.

    The orchestrator's copy was read when its run started, so writing it back whole would undo
    entries added, removed or updated in the meantime (including by other flows running at once).
    Without manifestNodes the supplied manifest is written as-is, as before.
    """
    nodes = manifest.get("nodes")
    if not nodes or existing is None:
        return manifest["content"]
    try:
        current = json.loads(existing.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError("invalid_manifest_file", "The base branch manifest is not valid JSON.") from error
    supplied = manifest["document"]
    if not isinstance(current, list) or not isinstance(supplied, list):
        raise PublicationError("invalid_manifest_file", "Manifests must be JSON arrays.")
    by_repository = {
        str(entry.get("github-repo", "")).rstrip("/"): entry for entry in supplied if isinstance(entry, dict)
    }
    for entry in current:
        if not isinstance(entry, dict):
            continue
        update = by_repository.get(str(entry.get("github-repo", "")).rstrip("/"))
        if update is None:
            continue
        for node in nodes:
            new, old = update.get(node), entry.get(node)
            # A changed path-to-scan means the scan state no longer applies; leave it so it is rescanned.
            if isinstance(new, dict) and isinstance(old, dict) and new.get("path-to-scan") == old.get("path-to-scan"):
                entry[node] = new
    return (json.dumps(current, indent=2) + "\n").encode("utf-8")


def publish(payload: dict[str, Any], client: GitHubClient | None = None, branch_factory: Callable[[], str] = default_branch_name) -> dict[str, Any]:
    request = validate_request(payload)
    github = client or GitHubClient(os.environ.get("GITHUB_PR_TOKEN", ""))
    repository = request["repository"]
    metadata = github.request("GET", f"/repos/{repository}")
    base_branch = request.get("baseBranch") or metadata.get("default_branch")
    if not isinstance(base_branch, str) or not base_branch:
        raise PublicationError("github_api_error", "GitHub did not return a default branch.")

    planned: list[PlannedFile] = []
    for item in request["schemas"]:
        existing = _existing_content(github, repository, item["path"], base_branch)
        action = "created" if existing is None else ("unchanged" if existing == item["content"] else "updated")
        planned.append(PlannedFile(item["path"], item["content"], action))
    if request["manifestFile"] is not None:
        item = request["manifestFile"]
        existing = _existing_content(github, repository, item["path"], base_branch)
        content = _merge_manifest(existing, item)
        action = "created" if existing is None else ("unchanged" if existing == content else "updated")
        planned.append(PlannedFile(item["path"], content, action))

    files_written = [{"path": item.path, "action": item.action} for item in planned]
    changed = [item for item in planned if item.action != "unchanged"]
    if not changed:
        return {
            "success": True, "status": "unchanged", "repository": repository, "branchName": "",
            "commitSha": "", "pullRequestUrl": "", "pullRequestNumber": 0,
            "filesWritten": files_written, "errors": [],
        }

    ref = github.request("GET", f"/repos/{repository}/git/ref/heads/{_encoded(base_branch)}")
    try:
        base_sha = ref["object"]["sha"]
    except (KeyError, TypeError) as error:
        raise PublicationError("github_api_error", "GitHub returned an invalid base branch reference.") from error
    commit = github.request("GET", f"/repos/{repository}/git/commits/{base_sha}")
    try:
        base_tree = commit["tree"]["sha"]
    except (KeyError, TypeError) as error:
        raise PublicationError("github_api_error", "GitHub returned an invalid base commit.") from error
    tree_items = []
    for item in changed:
        blob = github.request("POST", f"/repos/{repository}/git/blobs", {"content": base64.b64encode(item.content).decode("ascii"), "encoding": "base64"})
        tree_items.append({"path": item.path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = github.request("POST", f"/repos/{repository}/git/trees", {"base_tree": base_tree, "tree": tree_items})
    new_commit = github.request("POST", f"/repos/{repository}/git/commits", {
        "message": "Update generated database schemas", "tree": tree["sha"], "parents": [base_sha]
    })
    branch_name = request.get("branchName") or branch_factory()
    github.request("POST", f"/repos/{repository}/git/refs", {"ref": f"refs/heads/{branch_name}", "sha": new_commit["sha"]})
    pull_request = github.request("POST", f"/repos/{repository}/pulls", {
        "title": request.get("pullRequestTitle", "Update generated database schemas"),
        "head": branch_name,
        "base": base_branch,
        "body": request.get("pullRequestBody", "Generated by the database-schema workflow."),
    })
    return {
        "success": True, "status": "created", "repository": repository, "branchName": branch_name,
        "commitSha": new_commit["sha"], "pullRequestUrl": pull_request["html_url"],
        "pullRequestNumber": pull_request["number"], "filesWritten": files_written, "errors": [],
    }
