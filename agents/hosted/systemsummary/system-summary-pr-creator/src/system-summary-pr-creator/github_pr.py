"""Validate and publish one generated system-summaries file through the GitHub API."""

from __future__ import annotations

import base64
import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import github_app


MAX_CONTENT_BYTES = 10 * 1024 * 1024
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


class PublicationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        if not token:
            raise PublicationError("missing_github_token", "Neither the Atlas GitHub App nor GITHUB_PR_TOKEN is configured.")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, payload: object | None = None) -> Any:
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "system-summary-pr-creator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.api_url}{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
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
            raise PublicationError("github_api_error", message) from error
        except urllib.error.URLError as error:
            raise PublicationError("github_unavailable", "The GitHub API could not be reached.") from error
        return json.loads(response_body.decode("utf-8")) if response_body else {}


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
    allowed = {"repository", "targetPath", "fileContent", "baseBranch", "branchName", "pullRequestTitle", "pullRequestBody"}
    if not set(payload).issubset(allowed) or not {"repository", "targetPath", "fileContent"}.issubset(payload):
        raise PublicationError("invalid_input", "Input must contain repository, targetPath, and fileContent.")
    repository = normalize_repository(payload.get("repository"))
    target_path = _clean_relative_path(payload.get("targetPath"), "targetPath")
    if not target_path.endswith(".json"):
        raise PublicationError("invalid_input", "targetPath must end with .json.")
    try:
        content = (json.dumps(payload["fileContent"], indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PublicationError("invalid_content", "fileContent must be JSON serializable.") from error
    if len(content) > MAX_CONTENT_BYTES:
        raise PublicationError("content_too_large", "Generated content exceeds 10 MiB.")

    result: dict[str, Any] = {"repository": repository, "path": target_path, "content": content}
    for field in ("baseBranch", "branchName", "pullRequestTitle", "pullRequestBody"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise PublicationError("invalid_input", f"{field} must be a non-empty string.")
            result[field] = _validate_branch(value.strip(), field) if field in {"baseBranch", "branchName"} else value.strip()
    return result


def parse_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise PublicationError("invalid_json", "Input must be one JSON object.") from error
    if not isinstance(payload, dict):
        raise PublicationError("invalid_input", "Input must be one JSON object.")
    validate_request(payload)
    return payload


def default_branch_name() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"system-summary/{timestamp}-{uuid.uuid4().hex[:8]}"


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


def publish(payload: dict[str, Any], client: GitHubClient | None = None, branch_factory: Callable[[], str] = default_branch_name) -> dict[str, Any]:
    request = validate_request(payload)
    repository = request["repository"]
    if client is None:
        try:
            # A token for this one data repository only, minted from the tenant's App installation.
            client = GitHubClient(github_app.write_token(repository))
        except github_app.GitHubAppError as error:
            raise PublicationError("github_app_error", str(error)) from error
    github = client
    metadata = github.request("GET", f"/repos/{repository}")
    base_branch = request.get("baseBranch") or metadata.get("default_branch")
    if not isinstance(base_branch, str) or not base_branch:
        raise PublicationError("github_api_error", "GitHub did not return a default branch.")

    existing = _existing_content(github, repository, request["path"], base_branch)
    action = "created" if existing is None else ("unchanged" if existing == request["content"] else "updated")
    files_written = [{"path": request["path"], "action": action}]
    if action == "unchanged":
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
    blob = github.request(
        "POST", f"/repos/{repository}/git/blobs",
        {"content": base64.b64encode(request["content"]).decode("ascii"), "encoding": "base64"},
    )
    tree = github.request(
        "POST", f"/repos/{repository}/git/trees",
        {"base_tree": base_tree, "tree": [{"path": request["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]}]},
    )
    new_commit = github.request(
        "POST", f"/repos/{repository}/git/commits",
        {"message": "Update generated system summaries", "tree": tree["sha"], "parents": [base_sha]},
    )
    branch_name = request.get("branchName") or branch_factory()
    github.request("POST", f"/repos/{repository}/git/refs", {"ref": f"refs/heads/{branch_name}", "sha": new_commit["sha"]})
    pull_request = github.request(
        "POST", f"/repos/{repository}/pulls",
        {
            "title": request.get("pullRequestTitle", "Update generated system summaries"),
            "head": branch_name,
            "base": base_branch,
            "body": request.get("pullRequestBody", "Generated by the system-summary workflow."),
        },
    )
    return {
        "success": True, "status": "created", "repository": repository, "branchName": branch_name,
        "commitSha": new_commit["sha"], "pullRequestUrl": pull_request["html_url"],
        "pullRequestNumber": pull_request["number"], "filesWritten": files_written, "errors": [],
    }
