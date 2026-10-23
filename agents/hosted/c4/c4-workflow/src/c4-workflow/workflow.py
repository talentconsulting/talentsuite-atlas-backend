"""C4 discovery, generation, and publication orchestration."""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable


class WorkflowError(ValueError):
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
            result, _ = json.JSONDecoder().raw_decode(value[min(starts):])
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
        raise WorkflowError("invalid_source_url", "sourceUrl must match https://github.com/owner/repository/tree/ref[/path].")
    return parts[0], parts[1].removesuffix(".git"), parts[3], "/".join(parts[4:])


def parse_workflow_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise WorkflowError("invalid_json", "Input must be one JSON object.") from error
    if not isinstance(payload, dict):
        raise WorkflowError("invalid_input", "Input must be one JSON object.")
    allowed = {
        "sourceUrl", "targetRepository", "targetDirectory", "targetBaseBranch", "branchName",
        "pullRequestTitle", "pullRequestBody", "deferPublication",
        # The tenant's owner/repo data repository; discovery mints its GitHub read token for that tenant.
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


def _validate_blob_url(value: object, source: tuple[str, str, str, str]) -> str:
    if not isinstance(value, str):
        raise WorkflowError("invalid_discovery_output", "Discovered file URLs must be strings.")
    parsed = urllib.parse.urlparse(value)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) < 5
        or parts[2] != "blob"
        or (parts[0], parts[1].removesuffix(".git"), parts[3]) != source[:3]
    ):
        raise WorkflowError("invalid_discovery_output", "Discovered files must be blob URLs from the source repository and ref.")
    return value


def validate_discovery_output(value: Any, source_url: str, max_files: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"sourceFiles", "excludedFiles"}:
        raise WorkflowError("invalid_discovery_output", "Discovery must contain exactly sourceFiles and excludedFiles.")
    files = value["sourceFiles"]
    excluded = value["excludedFiles"]
    if not isinstance(files, list) or not files or len(files) > max_files:
        raise WorkflowError("invalid_discovery_output", f"sourceFiles must contain between 1 and {max_files} files.")
    source = parse_source_url(source_url)
    validated = [_validate_blob_url(item, source) for item in files]
    if len(validated) != len(set(validated)):
        raise WorkflowError("invalid_discovery_output", "sourceFiles must be unique.")
    if not isinstance(excluded, list) or any(
        not isinstance(item, dict)
        or set(item) != {"path", "reason"}
        or any(not isinstance(item[field], str) or not item[field] for field in item)
        for item in excluded
    ):
        raise WorkflowError("invalid_discovery_output", "excludedFiles has an invalid shape.")
    return {"sourceFiles": validated, "excludedFiles": excluded}


def validate_catalog(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise WorkflowError(str(error.get("code", "generation_failed")), str(error.get("message", "C4 generation failed.")))
    if not isinstance(value, dict) or set(value) != {"repository", "ref", "path", "c4Model", "diagrams"}:
        raise WorkflowError("invalid_generator_output", "Generator must return repository, ref, path, c4Model, and diagrams.")
    if any(not isinstance(value[field], str) for field in ("repository", "ref", "path")):
        raise WorkflowError("invalid_generator_output", "Generator returned invalid source fields.")
    if not isinstance(value["c4Model"], dict) or not isinstance(value["diagrams"], dict):
        raise WorkflowError("invalid_generator_output", "Generator returned invalid C4 fields.")
    return value


def invoke_agent(project: Any, agent_name: str, model: str, payload: dict[str, Any], max_attempts: int = 2) -> Any:
    last_error: Exception | None = None
    for _ in range(max(1, max_attempts)):
        try:
            response = project.get_openai_client(agent_name=agent_name).responses.create(
                model=model, input=json.dumps(payload, separators=(",", ":")), timeout=600
            )
            return parse_json_value(response.output_text)
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _publisher_payload(request: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    _, repository, _, _ = parse_source_url(request["sourceUrl"])
    directory = request.get("targetDirectory", f"{repository}/c4").strip("/")
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "catalogs": [{
            "sourceUrl": request["sourceUrl"],
            "catalog": catalog,
            "targetDirectory": directory,
        }],
    }
    for source, target in {
        "targetBaseBranch": "baseBranch", "branchName": "branchName",
        "pullRequestTitle": "pullRequestTitle", "pullRequestBody": "pullRequestBody",
    }.items():
        if source in request:
            payload[target] = request[source]
    return payload


def run_workflow(
    project: Any,
    request: dict[str, Any],
    discovery_name: str,
    generator_name: str,
    publisher_name: str,
    model: str,
    max_files: int = 150,
    generator_batch_size: int = 150,
    invoker: Callable[..., Any] = invoke_agent,
) -> dict[str, Any]:
    del generator_batch_size
    request = parse_workflow_request(json.dumps(request))
    source_url = request["sourceUrl"]
    discovered: dict[str, Any] | None = None
    try:
        discovery_request = {"sourceUrl": source_url, **({"tenant": request["tenant"]} if request.get("tenant") else {})}
        discovered = validate_discovery_output(invoker(project, discovery_name, model, discovery_request), source_url, max_files)
        catalog = validate_catalog(invoker(
            project,
            generator_name,
            model,
            {"sourceUrl": source_url, "sourceFiles": discovered["sourceFiles"]},
            max_attempts=1,
        ))
        owner, repository, ref, path = parse_source_url(source_url)
        if (catalog["repository"], catalog["ref"], catalog["path"]) != (f"{owner}/{repository}", ref, path):
            raise WorkflowError("source_identity_mismatch", "Generated catalog source identity does not match sourceUrl.")
    except Exception as error:
        return {
            "success": False,
            "sourceUrl": source_url,
            "generatedCatalogCount": 0,
            "discoveredFileCount": len(discovered["sourceFiles"]) if discovered else 0,
            "excludedFileCount": len(discovered["excludedFiles"]) if discovered else 0,
            "generationErrors": [{"errorType": type(error).__name__, "message": str(error)[:300]}],
            "catalogs": [],
            "pullRequest": None,
            "errors": [{"code": getattr(error, "code", "generation_failed"), "message": str(error)[:300]}],
        }
    catalog_items = [{"sourceUrl": source_url, "catalog": catalog}]
    common = {
        "sourceUrl": source_url,
        "generatedCatalogCount": 1,
        "discoveredFileCount": len(discovered["sourceFiles"]),
        "excludedFileCount": len(discovered["excludedFiles"]),
        "generationErrors": [],
        "catalogs": catalog_items,
        "errors": [],
    }
    if request.get("deferPublication", False):
        return {"success": True, **common, "pullRequest": None}
    publication = invoker(project, publisher_name, model, _publisher_payload(request, catalog), max_attempts=1)
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {"success": publication["success"], **common, "pullRequest": publication}
