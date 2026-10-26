"""Discovery-to-generation-to-pull-request orchestration."""

from __future__ import annotations

import concurrent.futures
import json
import re
import urllib.parse
from typing import Any, Callable


class WorkflowError(ValueError):
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
        decoder = json.JSONDecoder()
        try:
            result, _ = decoder.raw_decode(value[min(starts) :])
            return result
        except json.JSONDecodeError as error:
            raise WorkflowError("invalid_agent_response", "Agent response was not JSON.") from error


def parse_source_url(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("invalid_source_url", "sourceUrl must be a GitHub tree URL.")
    parsed = urllib.parse.urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WorkflowError("invalid_source_url", "sourceUrl must be a credential-free HTTPS GitHub URL.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "tree" or any(part in {".", ".."} for part in parts):
        raise WorkflowError(
            "invalid_source_url",
            "sourceUrl must match https://github.com/owner/repository/tree/ref[/path].",
        )
    return parts[0], parts[1].removesuffix(".git"), parts[3]


def parse_workflow_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise WorkflowError("invalid_json", "Input must be one JSON object.") from error
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
        "apiFiles",
        # The tenant's owner/repo data repository; discovery and the generator mint their GitHub read tokens for it.
        "tenant",
    }
    if not set(payload).issubset(allowed):
        raise WorkflowError("invalid_input", "Input contains unsupported properties.")
    parse_source_url(payload.get("sourceUrl"))
    defer_publication = payload.get("deferPublication", False)
    if not isinstance(defer_publication, bool):
        raise WorkflowError("invalid_input", "deferPublication must be a boolean.")
    target = payload.get("targetRepository")
    if not defer_publication and (not isinstance(target, str) or not target.strip()):
        raise WorkflowError("invalid_input", "targetRepository is required.")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise WorkflowError("invalid_input", "targetRepository must be a non-empty string.")
    for field in allowed - {"sourceUrl", "targetRepository", "deferPublication", "apiFiles"}:
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise WorkflowError("invalid_input", f"{field} must be a non-empty string.")
    if "apiFiles" in payload and not isinstance(payload["apiFiles"], list):
        raise WorkflowError("invalid_input", "apiFiles must be an array when supplied.")
    return payload


def _validate_blob_url(value: object, source: tuple[str, str, str]) -> str:
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
        or (parts[0], parts[1].removesuffix(".git"), parts[3]) != source
    ):
        raise WorkflowError(
            "invalid_discovery_output",
            "Discovered files must be GitHub blob URLs from the source repository and ref.",
        )
    return value


def validate_discovery_output(value: Any, source_url: str, max_files: int) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise WorkflowError(
            str(error.get("code", "discovery_failed")),
            str(error.get("message", "Source discovery failed.")),
        )
    if not isinstance(value, list):
        raise WorkflowError("invalid_discovery_output", "Discovery response must be a JSON array.")
    if len(value) > max_files:
        raise WorkflowError("too_many_api_files", f"Discovery returned more than {max_files} APIs.")
    source = parse_source_url(source_url)
    result = []
    api_urls: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"apiFile", "supportingFiles"}:
            raise WorkflowError(
                "invalid_discovery_output",
                f"Discovery element {index} must contain exactly apiFile and supportingFiles.",
            )
        api_file = _validate_blob_url(item["apiFile"], source)
        supporting = item["supportingFiles"]
        if not isinstance(supporting, list):
            raise WorkflowError("invalid_discovery_output", "supportingFiles must be an array.")
        supporting_files = [_validate_blob_url(file, source) for file in supporting]
        if len(supporting_files) != len(set(supporting_files)):
            raise WorkflowError("invalid_discovery_output", "supportingFiles must be unique.")
        if api_file in api_urls:
            raise WorkflowError("invalid_discovery_output", "apiFile values must be unique.")
        api_urls.add(api_file)
        result.append({"apiFile": api_file, "supportingFiles": supporting_files})
    return result


def validate_specification(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("openapi") != "3.1.0":
        raise WorkflowError("invalid_generator_output", "Generator response must be an OpenAPI 3.1.0 object.")
    for field in ("info", "paths", "components"):
        if not isinstance(value.get(field), dict):
            raise WorkflowError("invalid_generator_output", f"Generator response requires an object named {field}.")
    return value


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
                timeout=180,
            )
            return parse_json_value(response.output_text)
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _publisher_payload(request: dict[str, Any], specifications: list[dict[str, Any]]) -> dict[str, Any]:
    _, source_repository, _ = parse_source_url(request["sourceUrl"])
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "specifications": specifications,
        "targetDirectory": f"{source_repository}/open-api",
    }
    mappings = {
        "targetDirectory": "targetDirectory",
        "targetBaseBranch": "baseBranch",
        "branchName": "branchName",
        "pullRequestTitle": "pullRequestTitle",
        "pullRequestBody": "pullRequestBody",
    }
    for source, destination in mappings.items():
        if source in request:
            payload[destination] = request[source]
    return payload


def run_workflow(
    project: Any,
    request: dict[str, Any],
    discovery_name: str,
    generator_name: str,
    publisher_name: str,
    model: str,
    max_concurrency: int = 4,
    max_files: int = 100,
    invoker: Callable[..., Any] = invoke_agent,
) -> dict[str, Any]:
    request = parse_workflow_request(json.dumps(request))
    override = request.get("apiFiles")
    if override is not None:
        # A caller (the manifest orchestrator, batching a repository too large to generate in one
        # call) has already run discovery itself and is handing us exactly the slice to generate --
        # skip calling the discovery agent again.
        api_files = validate_discovery_output(override, request["sourceUrl"], max_files)
    else:
        discovery_request = {"sourceUrl": request["sourceUrl"], **({"tenant": request["tenant"]} if request.get("tenant") else {})}
        discovery = invoker(project, discovery_name, model, discovery_request)
        api_files = validate_discovery_output(discovery, request["sourceUrl"], max_files)
    if not api_files:
        # No candidate controller files means there is no REST API surface to document -- a
        # legitimate, stable result, not a failure. Report success with nothing generated so the
        # manifest's commit hash advances instead of rescanning this repository forever.
        return {
            "success": True,
            "sourceUrl": request["sourceUrl"],
            "discoveredCount": 0,
            "generatedCount": 0,
            "generationErrors": [],
            "specifications": [],
            "pullRequest": None,
            "errors": [],
        }

    generated: list[dict[str, Any] | None] = [None] * len(api_files)
    generation_errors: list[dict[str, str]] = []

    tenant = request.get("tenant")

    def generate(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        specification = invoker(project, generator_name, model, {**item, **({"tenant": tenant} if tenant else {})})
        return index, validate_specification(specification)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(max_concurrency, 8))) as executor:
        futures = {
            executor.submit(generate, index, item): (index, item)
            for index, item in enumerate(api_files)
        }
        for future in concurrent.futures.as_completed(futures):
            index, item = futures[future]
            try:
                result_index, specification = future.result()
                generated[result_index] = {
                    "apiFile": item["apiFile"],
                    "specification": specification,
                }
            except Exception as error:
                generation_errors.append(
                    {
                        "apiFile": item["apiFile"],
                        "errorType": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )

    specifications = [item for item in generated if item is not None]
    generation_errors.sort(key=lambda item: item["apiFile"])
    if not specifications:
        return {
            "success": False,
            "sourceUrl": request["sourceUrl"],
            "discoveredCount": len(api_files),
            "generatedCount": 0,
            "generationErrors": generation_errors,
            "pullRequest": None,
            "errors": [{"code": "generation_failed", "message": "No specifications were generated."}],
        }

    if request.get("deferPublication", False):
        # At least one specification generated successfully (checked above), so a partial batch
        # still counts as success -- surface the per-file failures as generationErrors rather than
        # discarding every specification that did generate.
        return {
            "success": True,
            "sourceUrl": request["sourceUrl"],
            "discoveredCount": len(api_files),
            "generatedCount": len(specifications),
            "generationErrors": generation_errors,
            "specifications": specifications,
            "pullRequest": None,
            "errors": [],
        }

    publication = invoker(
        project,
        publisher_name,
        model,
        _publisher_payload(request, specifications),
        max_attempts=1,
    )
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {
        "success": publication["success"],
        "sourceUrl": request["sourceUrl"],
        "discoveredCount": len(api_files),
        "generatedCount": len(specifications),
        "generationErrors": generation_errors,
        "pullRequest": publication,
        "errors": [],
    }
