""".NET version catalog generation and publication orchestration."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import github_app


class WorkflowError(RuntimeError):
    """A stable workflow validation or execution error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_json_value(text: str) -> Any:
    value = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE)
    if fence:
        value = fence.group(1).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError as original_error:
        starts = [position for position in (value.find("["), value.find("{")) if position >= 0]
        if not starts:
            raise WorkflowError("invalid_agent_response", "Agent response was not JSON.") from original_error
        try:
            result, _ = json.JSONDecoder().raw_decode(value[min(starts) :])
            return result
        except json.JSONDecodeError as error:
            raise WorkflowError("invalid_agent_response", "Agent response was not JSON.") from error


def parse_source_url(value: object) -> tuple[str, str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("invalid_source_url", "sourceUrl must be a GitHub tree URL.")
    parsed = urllib.parse.urlparse(value.strip())
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
        raise WorkflowError(
            "invalid_source_url",
            "sourceUrl must match https://github.com/owner/repository/tree/ref[/path].",
        )
    path = "/".join(parts[4:])
    return parts[0], parts[1].removesuffix(".git"), parts[3], path


def parse_workflow_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise WorkflowError("invalid_input", "Input must be one JSON object.") from error
    if not isinstance(payload, dict):
        raise WorkflowError("invalid_input", "Input must be one JSON object.")
    allowed = {
        "sourceUrl",
        "targetRepository",
        "targetDirectory",
        "targetBaseBranch",
        "branchName",
        "pullRequestTitle",
        "pullRequestBody",
        "deferPublication",
        # The tenant's owner/repo data repository; discovery, the generator and the commit-date lookup
        # mint their GitHub read tokens for that tenant.
        "tenant",
    }
    if not set(payload).issubset(allowed):
        raise WorkflowError("invalid_input", "Input contains unsupported properties.")
    parse_source_url(payload.get("sourceUrl"))
    deferred = payload.get("deferPublication", False)
    if not isinstance(deferred, bool):
        raise WorkflowError("invalid_input", "deferPublication must be a boolean.")
    target = payload.get("targetRepository")
    if not deferred and (not isinstance(target, str) or not target.strip()):
        raise WorkflowError("invalid_input", "targetRepository is required.")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise WorkflowError("invalid_input", "targetRepository must be a non-empty string.")
    for field in allowed - {"sourceUrl", "targetRepository", "deferPublication"}:
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise WorkflowError("invalid_input", f"{field} must be a non-empty string.")
    return payload


def _parse_blob_url(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise WorkflowError("invalid_discovery_output", "repoMetadataFiles must be strings.")
    parsed = urllib.parse.urlparse(value)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or len(parts) < 5 or parts[2] != "blob"
        or any(part in {".", ".."} or "/" in part or "\\" in part for part in parts)
    ):
        raise WorkflowError(
            "invalid_discovery_output",
            "repoMetadataFiles must contain GitHub blob URLs.",
        )
    return parts[0], parts[1].removesuffix(".git"), parts[3]


def validate_discovery_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise WorkflowError(
            str(error.get("code", "invalid_discovery_output")),
            str(error.get("message", ".NET version discovery failed.")),
        )
    if not isinstance(value, dict) or set(value) != {"repoMetadataFiles", "excludedFiles"}:
        raise WorkflowError(
            "invalid_discovery_output",
            "Discovery must contain exactly repoMetadataFiles and excludedFiles.",
        )
    files = value["repoMetadataFiles"]
    excluded = value["excludedFiles"]
    if not isinstance(files, list):
        raise WorkflowError("invalid_discovery_output", "repoMetadataFiles must be a list.")
    source_identity: tuple[str, str, str] | None = None
    for item in files:
        identity = _parse_blob_url(item)
        if source_identity is None:
            source_identity = identity
        elif identity != source_identity:
            raise WorkflowError(
                "invalid_discovery_output",
                "repoMetadataFiles must all belong to the same repository and ref.",
            )
    if len(set(files)) != len(files):
        raise WorkflowError("invalid_discovery_output", "repoMetadataFiles must be unique.")
    if not isinstance(excluded, list) or any(
        not isinstance(item, dict)
        or set(item) != {"path", "reason"}
        or not all(isinstance(item[key], str) and item[key] for key in item)
        for item in excluded
    ):
        raise WorkflowError("invalid_discovery_output", "excludedFiles has an invalid shape.")
    return {"repoMetadataFiles": list(files), "excludedFiles": excluded}


def validate_generator_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise WorkflowError(
            str(error.get("code", "generation_failed")),
            str(error.get("message", ".NET version generation failed.")),
        )
    if not isinstance(value, dict) or set(value) != {
        "repository",
        "ref",
        "path",
        "lastCommitDate",
        "projects",
        "sdks",
        "dotnetSupport",
    }:
        raise WorkflowError(
            "invalid_generator_output",
            "Generator response must contain repository, ref, path, lastCommitDate, projects, sdks, and dotnetSupport.",
        )
    if (
        not isinstance(value["repository"], str)
        or not isinstance(value["ref"], str)
        or not isinstance(value["path"], str)
    ):
        raise WorkflowError(
            "invalid_generator_output",
            "Generator response contains invalid repository, ref, or path values.",
        )
    if not isinstance(value["lastCommitDate"], str) or not value["lastCommitDate"]:
        raise WorkflowError(
            "invalid_generator_output",
            "Generator response contains an invalid lastCommitDate value.",
        )
    if not isinstance(value["projects"], list) or not isinstance(value["sdks"], list):
        raise WorkflowError(
            "invalid_generator_output",
            "Generator response contains invalid projects or sdks values.",
        )
    if not isinstance(value["dotnetSupport"], list):
        raise WorkflowError(
            "invalid_generator_output",
            "Generator response contains an invalid dotnetSupport value.",
        )
    return value


def invoke_agent(project: Any, agent_name: str, model: str, payload: dict[str, Any], max_attempts: int = 2) -> Any:
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


def merge_catalogs(catalogs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repository = catalogs[0]["repository"]
    ref = catalogs[0]["ref"]
    path = catalogs[0]["path"]
    last_commit_date = catalogs[0]["lastCommitDate"]
    warnings: list[dict[str, Any]] = []

    project_order: list[str] = []
    project_records: dict[str, dict[str, Any]] = {}
    sdk_order: list[str] = []
    sdk_records: dict[str, dict[str, Any]] = {}
    dotnet_support_records: dict[str, dict[str, Any]] = {}

    for catalog in catalogs:
        if (
            catalog["repository"] != repository
            or catalog["ref"] != ref
            or catalog["path"] != path
            or catalog["lastCommitDate"] != last_commit_date
        ):
            raise WorkflowError("source_identity_mismatch", "Generator batches disagree on source identity.")
        for project in catalog["projects"]:
            key = project["path"]
            if key not in project_records:
                project_order.append(key)
                project_records[key] = dict(project)
            elif project["targetFrameworks"] != project_records[key]["targetFrameworks"]:
                warnings.append({
                    "errorType": "DuplicateProject",
                    "message": f"Kept the first targetFrameworks for {key}; discarded a conflicting value from a later batch.",
                })
        for sdk in catalog["sdks"]:
            key = sdk["path"]
            if key not in sdk_records:
                sdk_order.append(key)
                sdk_records[key] = dict(sdk)
            elif sdk != sdk_records[key]:
                warnings.append({
                    "errorType": "DuplicateSdk",
                    "message": f"Kept the first version for {key}; discarded a conflicting value from a later batch.",
                })
        for entry in catalog["dotnetSupport"]:
            # Unlike projects/sdks, a given targetFramework moniker always produces the identical
            # (endOfSupport, supportPhase) pair regardless of which batch computed it -- it's a
            # pure function of the moniker string -- so this is a plain dedup, not a
            # first-wins-with-warning merge.
            dotnet_support_records[entry["targetFramework"]] = dict(entry)

    projects = sorted((project_records[key] for key in project_order), key=lambda item: item["path"].lower())
    sdks = sorted((sdk_records[key] for key in sdk_order), key=lambda item: item["path"].lower())
    dotnet_support = sorted(dotnet_support_records.values(), key=lambda item: item["targetFramework"])

    merged = {
        "repository": repository,
        "ref": ref,
        "path": path,
        "lastCommitDate": last_commit_date,
        "projects": projects,
        "sdks": sdks,
        "dotnetSupport": dotnet_support,
    }
    return merged, warnings


def _generate_batch(
    project: Any,
    generator_name: str,
    model: str,
    source_url: str,
    batch: list[str],
    invoker: Callable[..., Any],
    max_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
    tenant: str | None = None,
) -> dict[str, Any]:
    generator_request = {"sourceUrl": source_url, "sourceFiles": batch, **({"tenant": tenant} if tenant else {})}
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        if attempt:
            # Back off before retrying so concurrent batches do not hammer a throttled generator.
            time.sleep(retry_delay_seconds * 2 ** (attempt - 1))
        try:
            return validate_generator_output(
                invoker(project, generator_name, model, generator_request, max_attempts=1)
            )
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _publisher_payload(request: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    _, source_repository, _, _ = parse_source_url(request["sourceUrl"])
    target_directory = request.get("targetDirectory", f"{source_repository}/repo-metadata")
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "catalogs": [
            {
                "sourceUrl": request["sourceUrl"],
                "catalog": catalog,
                "targetPath": f"{target_directory.strip('/')}/repo-metadata.json",
            }
        ],
    }
    for source, destination in {
        "targetBaseBranch": "baseBranch",
        "branchName": "branchName",
        "pullRequestTitle": "pullRequestTitle",
        "pullRequestBody": "pullRequestBody",
    }.items():
        if source in request:
            payload[destination] = request[source]
    return payload


def _fetch_last_commit_date(owner: str, repository: str, ref: str, tenant: str | None = None) -> str:
    url = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repository, safe='')}/commits/{urllib.parse.quote(ref, safe='')}"
    )
    failure = WorkflowError(
        "commit_lookup_failed",
        f"Unable to determine the last commit date for {owner}/{repository}@{ref}.",
    )
    headers = {"User-Agent": "repo-metadata-workflow"}
    token = github_app.read_token(tenant)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise failure from error
    except urllib.error.URLError as error:
        raise failure from error
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise failure from error
    if not isinstance(payload, dict):
        raise failure
    commit = payload.get("commit")
    committer = commit.get("committer") if isinstance(commit, dict) else None
    author = commit.get("author") if isinstance(commit, dict) else None
    date = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(date, str) or not date:
        date = author.get("date") if isinstance(author, dict) else None
    if not isinstance(date, str) or not date:
        raise failure
    return date


def run_workflow(
    project: Any,
    request: dict[str, Any],
    discovery_name: str,
    generator_name: str,
    pr_creator_name: str,
    model: str,
    generator_batch_size: int = 5,
    generator_concurrency: int = 3,
    invoker: Callable[..., Any] = invoke_agent,
    retry_delay_seconds: float = 5.0,
) -> dict[str, Any]:
    request = parse_workflow_request(json.dumps(request))
    source_url = request["sourceUrl"]
    batch_errors: list[dict[str, Any]] = []
    try:
        discovery_request = {"sourceUrl": source_url, **({"tenant": request["tenant"]} if request.get("tenant") else {})}
        discovery = invoker(project, discovery_name, model, discovery_request)
        discovered = validate_discovery_output(discovery)
        files = discovered["repoMetadataFiles"]
        if not files:
            owner, repo, ref, path = parse_source_url(source_url)
            merged = {
                "repository": f"{owner}/{repo}",
                "ref": ref,
                "path": path,
                "lastCommitDate": _fetch_last_commit_date(owner, repo, ref, tenant=request.get("tenant")),
                "projects": [],
                "sdks": [],
                "dotnetSupport": [],
            }
        else:
            batches = [
                files[index : index + generator_batch_size]
                for index in range(0, len(files), generator_batch_size)
            ]
            with ThreadPoolExecutor(max_workers=max(1, generator_concurrency)) as executor:
                futures = [
                    executor.submit(
                        _generate_batch,
                        project,
                        generator_name,
                        model,
                        source_url,
                        batch,
                        invoker,
                        retry_delay_seconds=retry_delay_seconds,
                        tenant=request.get("tenant"),
                    )
                    for batch in batches
                ]
            # Collect in batch order, not completion order, so merge_catalogs keeps the same first occurrence.
            batch_catalogs = []
            for batch, future in zip(batches, futures):
                try:
                    batch_catalogs.append(future.result())
                except Exception as error:
                    batch_errors.append(
                        {
                            "sourceUrl": source_url,
                            "files": batch,
                            "errorType": type(error).__name__,
                            "message": str(error)[:300],
                        }
                    )
            if not batch_catalogs:
                raise WorkflowError("generation_failed", "No batch of source files produced a valid catalog.")
            merged, merge_warnings = merge_catalogs(batch_catalogs)
            batch_errors.extend(merge_warnings)
    except Exception as error:
        code = error.code if isinstance(error, WorkflowError) else "generation_failed"
        message = str(error) if isinstance(error, WorkflowError) else ".NET version generation failed."
        return {
            "success": False,
            "sourceUrl": source_url,
            "generatedCatalogCount": 0,
            "discoveredFileCount": 0,
            "excludedFileCount": 0,
            "generationErrors": batch_errors
            or [{"sourceUrl": source_url, "errorType": type(error).__name__, "message": str(error)[:300]}],
            "catalogs": [],
            "pullRequest": None,
            "errors": [{"code": code, "message": message}],
        }

    catalogs = [{"sourceUrl": source_url, "catalog": merged}]
    if request.get("deferPublication", False):
        return {
            "success": True,
            "sourceUrl": source_url,
            "generatedCatalogCount": 1,
            "discoveredFileCount": len(discovered["repoMetadataFiles"]),
            "excludedFileCount": len(discovered["excludedFiles"]),
            "generationErrors": batch_errors,
            "catalogs": catalogs,
            "pullRequest": None,
            "errors": [],
        }

    publication = invoker(
        project,
        pr_creator_name,
        model,
        _publisher_payload(request, merged),
        max_attempts=1,
    )
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {
        "success": publication["success"],
        "sourceUrl": source_url,
        "generatedCatalogCount": 1,
        "discoveredFileCount": len(discovered["repoMetadataFiles"]),
        "excludedFileCount": len(discovered["excludedFiles"]),
        "generationErrors": batch_errors,
        "catalogs": catalogs,
        "pullRequest": publication,
        "errors": [],
    }
