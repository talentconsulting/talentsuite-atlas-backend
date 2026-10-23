"""Manifest-driven database-schema workflow orchestration."""

from __future__ import annotations

import copy
import functools
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import github_app


MAX_MANIFEST_BYTES = 1024 * 1024
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DBSCHEMA_NODE = "dbschema"
LEGACY_DBSCHEMA_NODE = "db-schema"

class ManifestError(ValueError):
    """A stable manifest input, validation, or orchestration failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitHubBlob:
    owner: str
    repository: str
    ref: str
    path: str
    url: str

    @property
    def tenant(self) -> str:
        """The data repository holding the manifest identifies the tenant."""
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True)
class ManifestEntry:
    index: int
    owner: str
    repository: str
    repository_url: str
    ref: str
    scan_path: str
    path_to_scan: str
    last_commit: str
    manifest_node: str

    @property
    def source_url(self) -> str:
        return f"{self.repository_url}/{self.path_to_scan}"

    @property
    def repository_name(self) -> str:
        return f"{self.owner}/{self.repository}"


def parse_request(input_text: str) -> dict[str, str]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise ManifestError("invalid_json", "Input must be one JSON object.") from error
    if not isinstance(payload, dict) or set(payload) != {"sourceUrl"}:
        raise ManifestError("invalid_input", 'Input must contain exactly one "sourceUrl" property.')
    source_url = payload["sourceUrl"]
    if not isinstance(source_url, str) or not source_url.strip():
        raise ManifestError("invalid_input", "sourceUrl must be a non-empty string.")
    parse_blob_url(source_url.strip())
    return {"sourceUrl": source_url.strip()}


def parse_blob_url(value: str) -> GitHubBlob:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ManifestError("invalid_source_url", "sourceUrl must be a credential-free HTTPS GitHub blob URL.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] != "blob" or any(part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        raise ManifestError(
            "invalid_source_url",
            "sourceUrl must match https://github.com/owner/repository/blob/ref/path.json.",
        )
    owner, repository, _, ref, *path_parts = parts
    path = "/".join(path_parts)
    if not path.endswith(".json"):
        raise ManifestError("invalid_source_url", "sourceUrl must reference a JSON file.")
    return GitHubBlob(owner, repository.removesuffix(".git"), ref, path, value)


def _read_url(url: str, token: str | None = None, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "dbschema-manifest-orchestrator"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            declared_size = int(response.headers.get("Content-Length", "0") or "0")
            if declared_size > MAX_MANIFEST_BYTES:
                raise ManifestError("manifest_too_large", "The manifest exceeds 1 MiB.")
            body = response.read(MAX_MANIFEST_BYTES + 1)
    except ManifestError:
        raise
    except urllib.error.HTTPError as error:
        raise ManifestError("github_api_error", f"GitHub returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise ManifestError("github_unavailable", "GitHub could not be reached.") from error
    if len(body) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest_too_large", "The manifest exceeds 1 MiB.")
    return body


def download_manifest(blob: GitHubBlob) -> object:
    owner, repository = urllib.parse.quote(blob.owner, safe=""), urllib.parse.quote(blob.repository, safe="")
    token = github_app.read_token(blob.tenant)
    if token:
        # The contents API accepts App installation tokens; raw.githubusercontent.com is for anonymous reads.
        url = f"https://api.github.com/repos/{owner}/{repository}/contents/{urllib.parse.quote(blob.path, safe='/')}?ref={urllib.parse.quote(blob.ref, safe='')}"
        body = _read_url(url, token, accept="application/vnd.github.raw")
    else:
        body = _read_url(f"https://raw.githubusercontent.com/{owner}/{repository}/{urllib.parse.quote(blob.ref, safe='')}/{urllib.parse.quote(blob.path, safe='/')}")
    try:
        return json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ManifestError("invalid_manifest", "The manifest must be UTF-8 JSON.") from error
    except json.JSONDecodeError as error:
        raise ManifestError("invalid_manifest", "The manifest must contain valid JSON.") from error


def _parse_repository(value: object, label: str) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise ManifestError("invalid_manifest", f"{label} must be a GitHub repository URL.")
    parsed = urllib.parse.urlparse(value.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        raise ManifestError("invalid_manifest", f"{label} must be https://github.com/owner/repository.")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    return owner, repository, f"https://github.com/{owner}/{repository}"


def validate_manifest(value: object, max_entries: int) -> tuple[list[ManifestEntry], list[str]]:
    if not isinstance(value, list):
        raise ManifestError("invalid_manifest", "The manifest root must be an array.")
    if not value:
        raise ManifestError("invalid_manifest", "The manifest must contain at least one entry.")
    entries: list[ManifestEntry] = []
    repositories: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ManifestError("invalid_manifest", f"Manifest entry {index} has an invalid shape.")
        has_dbschema = DBSCHEMA_NODE in item
        has_legacy_dbschema = LEGACY_DBSCHEMA_NODE in item
        # Ignore entries owned by other domains.  `db-schema` is retained as a
        # read-compatible alias while manifests migrate to the `dbschema` name.
        if not has_dbschema and not has_legacy_dbschema:
            continue
        if has_dbschema and has_legacy_dbschema:
            raise ManifestError(
                "invalid_manifest",
                f"Manifest entry {index} must not contain both {DBSCHEMA_NODE} and {LEGACY_DBSCHEMA_NODE}.",
            )
        if "github-repo" not in item:
            raise ManifestError("invalid_manifest", f"Manifest entry {index} has an invalid shape.")
        manifest_node = DBSCHEMA_NODE if has_dbschema else LEGACY_DBSCHEMA_NODE
        dbschema = item[manifest_node]
        if not isinstance(dbschema, dict) or set(dbschema) != {"path-to-scan", "last-commit-hash-scanned"}:
            raise ManifestError("invalid_manifest", f"Manifest entry {index}.db-schema has an invalid shape.")
        owner, repository, repository_url = _parse_repository(item["github-repo"], f"Manifest entry {index}.github-repo")
        path_to_scan = dbschema["path-to-scan"]
        if not isinstance(path_to_scan, str):
            raise ManifestError("invalid_manifest", f"Manifest entry {index}.path-to-scan must be a string.")
        path_parts = [urllib.parse.unquote(part) for part in path_to_scan.strip("/").split("/")]
        if len(path_parts) < 2 or path_parts[0] != "tree" or any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in path_parts):
            raise ManifestError(
                "invalid_manifest",
                f"Manifest entry {index}.path-to-scan must match tree/ref[/path].",
            )
        last_commit = dbschema["last-commit-hash-scanned"]
        if not isinstance(last_commit, str) or (last_commit and not COMMIT_PATTERN.fullmatch(last_commit)):
            raise ManifestError(
                "invalid_manifest",
                f"Manifest entry {index}.last-commit-hash-scanned must be empty or a 40-character lowercase SHA.",
            )
        repository_name = f"{owner}/{repository}"
        if repository_name in repositories:
            raise ManifestError("invalid_manifest", f"Manifest repository {repository_name} is duplicated.")
        repositories.add(repository_name)
        entries.append(
            ManifestEntry(
                index=index,
                owner=owner,
                repository=repository,
                repository_url=repository_url,
                ref=path_parts[1],
                scan_path="/".join(path_parts[2:]),
                path_to_scan="/".join(path_parts),
                last_commit=last_commit,
                manifest_node=manifest_node,
            )
        )
    skipped_repositories: list[str] = []
    if len(entries) > max_entries:
        skipped_repositories = [entry.repository_name for entry in entries[max_entries:]]
        entries = entries[:max_entries]
    return entries, skipped_repositories


def _default_branch(entry: ManifestEntry, token: str | None = None) -> str:
    endpoint = (
        f"https://api.github.com/repos/{urllib.parse.quote(entry.owner, safe='')}/"
        f"{urllib.parse.quote(entry.repository, safe='')}"
    )
    response = json.loads(_read_url(endpoint, token).decode("utf-8"))
    branch = response.get("default_branch") if isinstance(response, dict) else None
    if not isinstance(branch, str) or not branch:
        raise ManifestError("github_api_error", f"GitHub did not report a default branch for {entry.repository_name}.")
    return branch


def latest_commit(entry: ManifestEntry, tenant: str | None = None) -> str:
    token = github_app.read_token(tenant)
    default_branch = _default_branch(entry, token)
    if entry.ref != default_branch:
        raise ManifestError(
            "invalid_manifest",
            f"Manifest entry for {entry.repository_name} scans ref {entry.ref!r}, but the repository's "
            f"default branch is {default_branch!r}. Only the default branch may be scanned.",
        )
    endpoint = (
        f"https://api.github.com/repos/{urllib.parse.quote(entry.owner, safe='')}/"
        f"{urllib.parse.quote(entry.repository, safe='')}/commits/{urllib.parse.quote(entry.ref, safe='')}"
    )
    response = json.loads(_read_url(endpoint, token).decode("utf-8"))
    sha = response.get("sha") if isinstance(response, dict) else None
    if not isinstance(sha, str) or not COMMIT_PATTERN.fullmatch(sha):
        raise ManifestError("github_api_error", f"GitHub returned an invalid commit for {entry.repository_name}.")
    return sha


def parse_json_value(text: str) -> Any:
    value = text.strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError as original_error:
        starts = [position for position in (value.find("["), value.find("{")) if position >= 0]
        if not starts:
            raise ManifestError("invalid_agent_response", "Agent response was not JSON.") from original_error
        try:
            result, _ = json.JSONDecoder().raw_decode(value[min(starts) :])
            return result
        except json.JSONDecodeError as error:
            raise ManifestError("invalid_agent_response", "Agent response was not JSON.") from error


def invoke_agent(
    project: Any,
    agent_name: str,
    model: str,
    payload: dict[str, Any],
    max_attempts: int = 2,
) -> Any:
    last_error: Exception | None = None
    for _ in range(max(1, max_attempts)):
        try:
            client = project.get_openai_client(agent_name=agent_name)
            response = client.responses.create(
                model=model,
                input=json.dumps(payload, separators=(",", ":")),
                timeout=600,
            )
            return parse_json_value(response.output_text)
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _validate_workflow_output(value: Any, entry: ManifestEntry) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("success"), bool):
        raise ManifestError("invalid_workflow_output", "Database-schema workflow returned an invalid response.")
    if not value["success"]:
        errors = value.get("generationErrors") or value.get("errors") or []
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else "Schema generation failed."
        raise ManifestError("schema_generation_failed", str(detail))
    schemas = value.get("schemas")
    if not isinstance(schemas, list) or len(schemas) != 1:
        raise ManifestError("invalid_workflow_output", "Database-schema workflow must return exactly one schema.")
    item = schemas[0]
    if not isinstance(item, dict) or set(item) != {"sourceUrl", "schema"}:
        raise ManifestError("invalid_workflow_output", "Database-schema workflow returned an invalid schema item.")
    if item["sourceUrl"] != entry.source_url:
        raise ManifestError("invalid_workflow_output", "Database-schema workflow returned a schema for another source.")
    schema = item["schema"]
    if not isinstance(schema, dict) or set(schema) != {"database", "tables", "types"}:
        raise ManifestError("invalid_workflow_output", "Database-schema workflow returned an invalid schema.")
    if not isinstance(schema["database"], dict) or not isinstance(schema["tables"], list) or not isinstance(schema["types"], list):
        raise ManifestError("invalid_workflow_output", "Database-schema workflow returned an incomplete schema.")
    return [
        {
            "sourceUrl": item["sourceUrl"],
            "schema": schema,
            "targetPath": f"{entry.repository}/db-schema/database.schema.json",
        }
    ]


def run_manifest(
    project: Any,
    request: dict[str, str],
    workflow_name: str,
    publisher_name: str,
    model: str,
    max_entries: int = 25,
    max_schemas: int = 100,
    workflow_concurrency: int = 4,
    manifest_loader: Callable[[GitHubBlob], object] = download_manifest,
    commit_resolver: Callable[[ManifestEntry], str] | None = None,
    invoker: Callable[..., Any] = invoke_agent,
) -> dict[str, Any]:
    blob = parse_blob_url(request["sourceUrl"])
    # The manifest's repository is the tenant: every GitHub token in this run is minted for it.
    commit_resolver = commit_resolver or functools.partial(latest_commit, tenant=blob.tenant)
    manifest = manifest_loader(blob)
    entries, skipped_repositories = validate_manifest(manifest, max_entries)
    updated_manifest = copy.deepcopy(manifest)
    up_to_date: list[dict[str, str]] = []
    changed: list[tuple[ManifestEntry, str]] = []
    failures: list[dict[str, str]] = []

    for entry in entries:
        try:
            commit = commit_resolver(entry)
            if commit == entry.last_commit:
                up_to_date.append({"repository": entry.repository_name, "commit": commit})
            else:
                changed.append((entry, commit))
        except Exception as error:
            failures.append(
                {
                    "repository": entry.repository_name,
                    "stage": "commit_check",
                    "errorType": type(error).__name__,
                    "message": str(error)[:300],
                }
            )

    if not changed:
        return {
            "success": not failures,
            "status": "up_to_date" if not failures else "failed",
            "sourceUrl": request["sourceUrl"],
            "checkedCount": len(entries),
            "changedCount": 0,
            "truncated": bool(skipped_repositories),
            "skippedCount": len(skipped_repositories),
            "skippedRepositories": skipped_repositories,
            "generatedRepositoryCount": 0,
            "generatedSchemaCount": 0,
            "upToDate": up_to_date,
            "failures": failures,
            "pullRequest": None,
        }

    combined_schemas: list[dict[str, Any]] = []
    generated_repositories: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workflow_concurrency)) as executor:
        futures = [
            executor.submit(
                invoker,
                project,
                workflow_name,
                model,
                {"sourceUrl": entry.source_url, "deferPublication": True, "tenant": blob.tenant},
            )
            for entry, _commit in changed
        ]
    # Process results in manifest order so the schema cap, hash updates, and PR content stay deterministic.
    for (entry, commit), future in zip(changed, futures):
        try:
            workflow_result = future.result()
            schemas = _validate_workflow_output(workflow_result, entry)
            if len(combined_schemas) + len(schemas) > max_schemas:
                raise ManifestError("too_many_schemas", f"A run may publish at most {max_schemas} database schemas.")
            combined_schemas.extend(schemas)
            updated_manifest[entry.index][entry.manifest_node]["last-commit-hash-scanned"] = commit
            warnings = [
                {"errorType": warning.get("errorType", "GenerationWarning"), "message": str(warning.get("message", ""))[:300]}
                for warning in (workflow_result.get("generationErrors") or [])
                if isinstance(warning, dict)
            ]
            generated_repositories.append({"repository": entry.repository_name, "commit": commit, "warnings": warnings})
        except Exception as error:
            failures.append(
                {
                    "repository": entry.repository_name,
                    "stage": "schema_workflow",
                    "errorType": type(error).__name__,
                    "message": str(error)[:300],
                }
            )

    if not combined_schemas:
        return {
            "success": False,
            "status": "failed",
            "sourceUrl": request["sourceUrl"],
            "checkedCount": len(entries),
            "changedCount": len(changed),
            "truncated": bool(skipped_repositories),
            "skippedCount": len(skipped_repositories),
            "skippedRepositories": skipped_repositories,
            "generatedRepositoryCount": 0,
            "generatedSchemaCount": 0,
            "upToDate": up_to_date,
            "failures": failures,
            "pullRequest": None,
        }

    publication = invoker(
        project,
        publisher_name,
        model,
        {
            "repository": f"{blob.owner}/{blob.repository}",
            "schemas": combined_schemas,
            "baseBranch": blob.ref,
            "manifestFile": {"path": blob.path, "content": updated_manifest},
            # The PR creator applies only these nodes to the manifest as it is when the PR is opened.
            "manifestNodes": [DBSCHEMA_NODE, LEGACY_DBSCHEMA_NODE],
            "pullRequestTitle": "Update generated database schemas",
            "pullRequestBody": (
                "Generated database schemas for repositories whose branch-head commit changed. "
                "The manifest commit hashes are updated in the same pull request."
            ),
        },
        max_attempts=1,
    )
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise ManifestError("invalid_publisher_output", "PR creator returned an invalid response.")
    return {
        "success": publication["success"] and not failures,
        "status": "created" if publication["success"] else "failed",
        "sourceUrl": request["sourceUrl"],
        "checkedCount": len(entries),
        "changedCount": len(changed),
        "truncated": bool(skipped_repositories),
        "skippedCount": len(skipped_repositories),
        "skippedRepositories": skipped_repositories,
        "generatedRepositoryCount": len(generated_repositories),
        "generatedSchemaCount": len(combined_schemas),
        "generatedRepositories": generated_repositories,
        "upToDate": up_to_date,
        "failures": failures,
        "pullRequest": publication,
    }
