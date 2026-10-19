"""User-journeys generation and publication orchestration.

discovery (deterministic navigation graph) -> generator "describe" per batch (purposes, conditions)
-> generator "journeys" (named journeys, validated against the graph) -> PR creator.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


CATALOG_FIELDS = (
    "repository", "ref", "path", "webProjects", "pages", "transitions", "siteNavigation",
    "unresolved", "selfLinkCount", "journeys", "journeyWarnings",
)


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
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or len(parts) < 4 or parts[2] != "tree"
        or any(part in {".", ".."} or "/" in part or "\\" in part for part in parts)
    ):
        raise WorkflowError("invalid_source_url", "sourceUrl must match https://github.com/owner/repository/tree/ref[/path].")
    return parts[0], parts[1].removesuffix(".git"), parts[3], "/".join(parts[4:])


def parse_workflow_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise WorkflowError("invalid_input", "Input must be one JSON object.") from error
    if not isinstance(payload, dict):
        raise WorkflowError("invalid_input", "Input must be one JSON object.")
    allowed = {"sourceUrl", "targetRepository", "targetDirectory", "targetBaseBranch", "branchName",
               "pullRequestTitle", "pullRequestBody", "deferPublication"}
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


def _raise_agent_error(value: Any, default_code: str) -> None:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise WorkflowError(str(error.get("code", default_code)), str(error.get("message", "Agent call failed.")))


def validate_discovery_output(value: Any) -> dict[str, Any]:
    _raise_agent_error(value, "discovery_failed")
    required = {"repository", "ref", "path", "webProjects", "graph", "batches", "excludedFiles"}
    if not isinstance(value, dict) or set(value) != required:
        raise WorkflowError("invalid_discovery_output", "Discovery must contain exactly " + ", ".join(sorted(required)) + ".")
    graph = value["graph"]
    if not isinstance(graph, dict) or not all(isinstance(graph.get(k), list) for k in ("pages", "transitions", "siteNavigation", "unresolved")):
        raise WorkflowError("invalid_discovery_output", "Discovery graph is incomplete.")
    page_ids = {p.get("id") for p in graph["pages"] if isinstance(p, dict)}
    if len(page_ids) != len(graph["pages"]) or any(t.get("from") not in page_ids or t.get("to") not in page_ids for t in graph["transitions"]):
        raise WorkflowError("invalid_discovery_output", "Discovery transitions must connect known, uniquely named pages.")
    if not isinstance(value["batches"], list) or sorted(pid for b in value["batches"] for pid in b.get("pageIds", [])) != sorted(page_ids):
        raise WorkflowError("invalid_discovery_output", "Discovery batches must cover every page exactly once.")
    return value


def invoke_agent(project: Any, agent_name: str, model: str, payload: dict[str, Any], max_attempts: int = 2) -> Any:
    last_error: Exception | None = None
    for _ in range(max(1, max_attempts)):
        try:
            client = project.get_openai_client(agent_name=agent_name)
            response = client.responses.create(model=model, input=json.dumps(payload, separators=(",", ":")), timeout=600)
            return parse_json_value(response.output_text)
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _call_generator(project, generator_name, model, payload, invoker, max_attempts=3, retry_delay_seconds=5.0) -> dict:
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        if attempt:
            time.sleep(retry_delay_seconds * 2 ** (attempt - 1))
        try:
            value = invoker(project, generator_name, model, payload, max_attempts=1)
            _raise_agent_error(value, "generation_failed")
            if not isinstance(value, dict):
                raise WorkflowError("invalid_generator_output", "Generator returned an invalid response.")
            return value
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _describe(project, generator_name, model, source_url, graph, batches, concurrency, invoker, retry_delay_seconds):
    transitions_by_page: dict[str, list[dict]] = {}
    for t in graph["transitions"]:
        transitions_by_page.setdefault(t["from"], []).append({"from": t["from"], "to": t["to"], "trigger": t["trigger"]})
    pages = {p["id"]: p for p in graph["pages"]}
    payloads = [{
        "mode": "describe", "sourceUrl": source_url,
        "pages": [{**{k: pages[pid][k] for k in ("id", "routes", "kind", "sourceFile")},
                   "fields": [{k: f.get(k) for k in ("name", "control", "label", "required", "constraints")}
                              for f in pages[pid].get("fields", [])]}
                  for pid in batch["pageIds"]],
        "transitions": [t for pid in batch["pageIds"] for t in transitions_by_page.get(pid, [])],
        "sourceFiles": batch["files"],
    } for batch in batches]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [executor.submit(_call_generator, project, generator_name, model, payload, invoker,
                                   retry_delay_seconds=retry_delay_seconds) for payload in payloads]
    purposes, conditions, validations, errors = {}, {}, {}, []
    for batch, future in zip(batches, futures):   # batch order, so results are deterministic
        try:
            result = future.result()
            for page in result.get("pages", []):
                if page.get("id") in pages:
                    purposes[page["id"]] = page.get("purpose")
                    for field in page.get("fields") or []:
                        validations[(page["id"], field.get("name"))] = field.get("validation")
            for t in result.get("transitions", []):
                conditions[(t.get("from"), t.get("to"), t.get("trigger"))] = t.get("condition")
        except Exception as error:
            errors.append({"stage": "describe", "pageIds": batch["pageIds"][:20], "errorType": type(error).__name__,
                           "message": str(error)[:300]})
    return purposes, conditions, validations, errors


def build_catalog(discovery: dict, purposes: dict, conditions: dict, journeys: dict, validations: dict | None = None) -> dict[str, Any]:
    validations = validations or {}
    graph = discovery["graph"]
    return {
        "repository": discovery["repository"],
        "ref": discovery["ref"],
        "path": discovery["path"],
        "webProjects": discovery["webProjects"],
        "pages": [{**page, "purpose": purposes.get(page["id"], page.get("purpose")),
                   "fields": [{**field, "validation": validations.get((page["id"], field["name"]), field.get("validation"))}
                              for field in page.get("fields", [])]}
                  for page in graph["pages"]],
        "transitions": [{**t, "condition": conditions.get((t["from"], t["to"], t["trigger"]), t.get("condition"))}
                        for t in graph["transitions"]],
        "siteNavigation": graph["siteNavigation"],
        "unresolved": graph["unresolved"],
        "selfLinkCount": graph.get("selfLinkCount", 0),
        "journeys": journeys.get("journeys", []),
        "journeyWarnings": journeys.get("warnings", []),
    }


def _publisher_payload(request: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    _, source_repository, _, _ = parse_source_url(request["sourceUrl"])
    target_directory = request.get("targetDirectory", f"{source_repository}/user-journeys")
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "catalogs": [{"sourceUrl": request["sourceUrl"], "catalog": catalog,
                      "targetPath": f"{target_directory.strip('/')}/user-journeys.json"}],
    }
    for source, destination in {"targetBaseBranch": "baseBranch", "branchName": "branchName",
                                 "pullRequestTitle": "pullRequestTitle", "pullRequestBody": "pullRequestBody"}.items():
        if source in request:
            payload[destination] = request[source]
    return payload


def _result(success, source_url, catalog=None, errors=(), generation_errors=(), pull_request=None) -> dict[str, Any]:
    return {
        "success": success,
        "sourceUrl": source_url,
        "generatedCatalogCount": 1 if catalog is not None else 0,
        "pageCount": len(catalog["pages"]) if catalog else 0,
        "journeyCount": len(catalog["journeys"]) if catalog else 0,
        "generationErrors": list(generation_errors),
        "catalogs": [{"sourceUrl": source_url, "catalog": catalog}] if catalog is not None else [],
        "pullRequest": pull_request,
        "errors": list(errors),
    }


def run_workflow(
    project: Any,
    request: dict[str, Any],
    discovery_name: str,
    generator_name: str,
    pr_creator_name: str,
    model: str,
    generator_concurrency: int = 3,
    invoker: Callable[..., Any] = invoke_agent,
    retry_delay_seconds: float = 5.0,
) -> dict[str, Any]:
    request = parse_workflow_request(json.dumps(request))
    source_url = request["sourceUrl"]
    generation_errors: list[dict[str, Any]] = []
    try:
        discovery = validate_discovery_output(invoker(project, discovery_name, model, {"sourceUrl": source_url}))
        graph = discovery["graph"]
        purposes, conditions, validations, journeys = {}, {}, {}, {"journeys": [], "warnings": []}
        if graph["pages"]:
            purposes, conditions, validations, describe_errors = _describe(
                project, generator_name, model, source_url, graph, discovery["batches"],
                generator_concurrency, invoker, retry_delay_seconds)
            generation_errors += describe_errors
            if describe_errors and len(describe_errors) == len(discovery["batches"]):
                # Every model call failed (an outage, say): fail so the commit is not recorded and the next run retries.
                raise WorkflowError("generation_failed", "No batch of pages could be described.")
            described = [{"id": p["id"], "routes": p["routes"], "purpose": purposes.get(p["id"]),
                          "requiresAuthentication": p["requiresAuthentication"], "isEntryPoint": p["isEntryPoint"]}
                         for p in graph["pages"]]
            edges = [{"from": t["from"], "to": t["to"], "trigger": t["trigger"],
                      "condition": conditions.get((t["from"], t["to"], t["trigger"]))} for t in graph["transitions"]]
            try:
                journeys = _call_generator(project, generator_name, model,
                                           {"mode": "journeys", "repository": discovery["repository"],
                                            "pages": described, "transitions": edges},
                                           invoker, retry_delay_seconds=retry_delay_seconds)
            except Exception as error:
                generation_errors.append({"stage": "journeys", "errorType": type(error).__name__, "message": str(error)[:300]})
        catalog = build_catalog(discovery, purposes, conditions, journeys, validations)
    except Exception as error:
        code = error.code if isinstance(error, WorkflowError) else "generation_failed"
        message = str(error) if isinstance(error, WorkflowError) else "User-journeys generation failed."
        return _result(False, source_url, errors=[{"code": code, "message": message}],
                       generation_errors=generation_errors or [{"errorType": type(error).__name__, "message": str(error)[:300]}])

    if request.get("deferPublication", False):
        return _result(True, source_url, catalog, generation_errors=generation_errors)
    publication = invoker(project, pr_creator_name, model, _publisher_payload(request, catalog), max_attempts=1)
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return _result(publication["success"], source_url, catalog, generation_errors=generation_errors, pull_request=publication)
