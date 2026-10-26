"""Service-dependency discovery, generation, merging, and publication orchestration."""

from __future__ import annotations

import concurrent.futures
import copy
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
        # The tenant's owner/repo data repository; discovery and the generator mint their GitHub read tokens for it.
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
    if not isinstance(files, list) or len(files) > max_files:
        raise WorkflowError("invalid_discovery_output", f"sourceFiles must contain at most {max_files} files.")
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
        raise WorkflowError(str(error.get("code", "generation_failed")), str(error.get("message", "Catalog generation failed.")))
    if not isinstance(value, dict) or set(value) != {"repository", "ref", "path", "systemName", "containers", "dependencies"}:
        raise WorkflowError(
            "invalid_generator_output",
            "Generator must return repository, ref, path, systemName, containers, and dependencies.",
        )
    if (
        any(not isinstance(value[field], str) for field in ("repository", "ref", "path", "systemName"))
        or not isinstance(value["containers"], list)
        or not isinstance(value["dependencies"], list)
    ):
        raise WorkflowError("invalid_generator_output", "Generator returned invalid catalog fields.")
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


def _merge_scalar(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    field: str,
    dependency_name: str,
    unknown: str | None = None,
) -> None:
    current = existing.get(field)
    incoming = candidate.get(field)
    if current in {None, unknown} and incoming not in {None, unknown}:
        existing[field] = incoming
    elif incoming not in {None, unknown} and current not in {None, unknown} and incoming != current:
        # Different source batches often describe the same API metadata with aliases
        # (for example, "API Key" and "api-key"). Keep the first deterministic
        # value and continue merging the stronger evidence instead of failing the
        # complete repository catalog over a presentation-level disagreement.
        return


# Different batches only see a subset of files, so the same real container or dependency can come
# back with a slightly different name each time (a "V2" namespace token, spacing/casing, or -- for
# dependencies -- a generic Client/ApiClient/Api/DbContext suffix on an interface vs. implementation
# vs. ORM class vs. friendly name). Merge identity is computed on a normalized form of the name so
# these still collapse into one entry, while the record actually kept in the output uses whichever raw
# name was seen first.
_INTERFACE_PREFIX_RE = re.compile(r"^I(?=[A-Z])")
_VERSION_TOKEN_RE = re.compile(r"v\d+")
_GENERIC_DEPENDENCY_SUFFIX_RE = re.compile(r"(?:apiclient|client|dbcontext|context|api)$")


def _normalize_name(name: object) -> str:
    text = _INTERFACE_PREFIX_RE.sub("", str(name or "").strip())
    normalized = _VERSION_TOKEN_RE.sub("", text.lower())
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _normalize_dependency_identity_name(name: object) -> str:
    normalized = _normalize_name(name)
    return _GENERIC_DEPENDENCY_SUFFIX_RE.sub("", normalized) or normalized


def _container_identity(container: dict[str, Any]) -> tuple[str, str]:
    return (str(container.get("type") or ""), _normalize_name(container.get("name")))


def _unique_container_id(candidate: str, used: set[str]) -> str:
    # Each batch only guarantees unique ids within its own containers list, so two batches can each
    # independently pick the same id (e.g. "commitments") for two genuinely different containers (a
    # "job" one and an "api" one) -- disambiguate before adding either to the merged result so no two
    # containers, and therefore no two distinct sourceId references, ever collide.
    unique = candidate
    suffix = 2
    while unique in used:
        unique = f"{candidate}-{suffix}"
        suffix += 1
    used.add(unique)
    return unique


def merge_catalogs(catalogs: list[dict[str, Any]]) -> dict[str, Any]:
    first = catalogs[0]
    result = {
        "repository": first["repository"], "ref": first["ref"], "path": first["path"],
        "systemName": first["systemName"], "containers": [], "dependencies": [],
    }
    containers_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    used_container_ids: set[str] = set()
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    for catalog in catalogs:
        if (catalog["repository"], catalog["ref"], catalog["path"]) != (result["repository"], result["ref"], result["path"]):
            raise WorkflowError("source_identity_mismatch", "Generator batches returned different source identities.")
        # Each batch only sees a subset of files, so the model may not choose the same container id for
        # the same real container across batches -- merge containers by (type, name) instead, and remap
        # this batch's dependencies onto the merged container's id.
        container_id_map: dict[str, str] = {}
        for container in catalog["containers"]:
            identity = _container_identity(container)
            existing_container = containers_by_identity.get(identity)
            if existing_container is None:
                copied = copy.deepcopy(container)
                copied["id"] = _unique_container_id(copied["id"], used_container_ids)
                containers_by_identity[identity] = copied
                result["containers"].append(copied)
                container_id_map[container["id"]] = copied["id"]
                continue
            container_id_map[container["id"]] = existing_container["id"]
            evidence = {json.dumps(item, sort_keys=True): item for item in existing_container["evidence"]}
            for item in container["evidence"]:
                evidence.setdefault(json.dumps(item, sort_keys=True), copy.deepcopy(item))
            existing_container["evidence"] = [evidence[key] for key in sorted(evidence)]
        for dependency in catalog["dependencies"]:
            dependency["sourceId"] = container_id_map.get(dependency["sourceId"], dependency["sourceId"])
            kind = str(dependency.get("kind") or "")
            # There is reliably at most one database per repository, so unlike other kinds a
            # database dependency's name is not part of its merge identity -- every database-kind
            # entry for the same container collapses into one, regardless of what it's named.
            name_for_identity = "" if kind == "database" else _normalize_dependency_identity_name(dependency.get("name"))
            identity = (dependency["sourceId"], kind, name_for_identity)
            existing = seen.get(identity)
            if existing is None:
                copied = copy.deepcopy(dependency)
                seen[identity] = copied
                result["dependencies"].append(copied)
                continue
            _merge_scalar(existing, dependency, "classification", existing["name"], "unknown")
            _merge_scalar(existing, dependency, "direction", existing["name"], "unknown")
            _merge_scalar(existing, dependency, "client", existing["name"])
            _merge_scalar(existing, dependency, "technology", existing["name"])
            _merge_scalar(existing["authentication"], dependency["authentication"], "type", existing["name"])
            for owner, field in ((existing, "configurationKeys"), (existing["authentication"], "configurationKeys")):
                candidate_owner = dependency if owner is existing else dependency["authentication"]
                owner[field] = sorted(set(owner[field]) | set(candidate_owner[field]), key=str.lower)
            for field in ("operations", "resources", "evidence"):
                indexed = {json.dumps(item, sort_keys=True): item for item in existing[field]}
                for item in dependency[field]:
                    indexed.setdefault(json.dumps(item, sort_keys=True), copy.deepcopy(item))
                existing[field] = [indexed[key] for key in sorted(indexed)]
            if confidence_rank[dependency["confidence"]] > confidence_rank[existing["confidence"]]:
                existing["confidence"] = dependency["confidence"]
    # Different containers can each have their own relationship row to the database, but since there
    # is reliably at most one database per repository, every row must agree on its name/targetId --
    # otherwise two containers' rows for the same physical database would look like different
    # databases. Canonicalize on whichever row was encountered first.
    database_entries = [item for item in result["dependencies"] if item["kind"] == "database"]
    for entry in database_entries[1:]:
        entry["name"] = database_entries[0]["name"]
        entry["targetId"] = database_entries[0]["targetId"]
    result["containers"].sort(key=lambda item: item["id"])
    result["dependencies"].sort(key=lambda item: (item["kind"], item["name"].lower()))
    return result


# A C4-PlantUML diagram declares each node once and references it by alias from any number of Rel()
# lines, so it renders correctly from the same edge-list shape (one dependency row per container that
# evidences it, sharing a targetId) that trips up naive JSON viewers into drawing one box per row.
_C4_INCLUDE = "!include https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Container.puml"
_CONTAINER_TECHNOLOGY_BY_TYPE = {"api": "API", "web": "Web Application", "job": "Background Job", "other": "Component"}
_EXTERNAL_MACRO_BY_KIND = {
    "http-api": "System_Ext",
    "grpc-service": "System_Ext",
    "cloud-service": "System_Ext",
    "cache": "SystemDb_Ext",
    "database": "SystemDb_Ext",
    "object-storage": "SystemDb_Ext",
    "message-broker": "SystemQueue_Ext",
}
_PUML_ALIAS_RE = re.compile(r"[^A-Za-z0-9_]")


def _puml_alias(prefix: str, value: str) -> str:
    return prefix + _PUML_ALIAS_RE.sub("_", value)


def _puml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _puml_node(macro: str, alias: str, name: str, technology: str | None) -> str:
    args = f'{alias}, "{_puml_escape(name)}"'
    if technology:
        args += f', "{_puml_escape(technology)}"'
    return f"{macro}({args})"


def catalog_to_puml(catalog: dict[str, Any]) -> str:
    lines = [
        "@startuml",
        _C4_INCLUDE,
        "LAYOUT_WITH_LEGEND()",
        "",
        f'System_Boundary({_puml_alias("sys_", catalog["repository"])}, "{_puml_escape(catalog["systemName"])}") {{',
    ]
    for item in catalog["containers"]:
        technology = _CONTAINER_TECHNOLOGY_BY_TYPE.get(item["type"], item["type"])
        lines.append("  " + _puml_node("Container", _puml_alias("c_", item["id"]), item["name"], technology))
    lines.append("}")

    # Each dependency row is one container's relationship to a target; several rows commonly share a
    # targetId (several containers using the same database). Declare the target node once from the
    # first row that references it, then let every row become its own Rel() -- multiple edges into one
    # node is exactly what C4-PlantUML expects, unlike a viewer that draws one box per row.
    declared: set[str] = set()
    node_lines: list[str] = []
    rel_lines: list[str] = []
    for dependency in catalog["dependencies"]:
        target_alias = _puml_alias("d_", dependency["targetId"])
        if target_alias not in declared:
            declared.add(target_alias)
            macro = _EXTERNAL_MACRO_BY_KIND.get(dependency["kind"], "System_Ext")
            node_lines.append(_puml_node(macro, target_alias, dependency["name"], dependency.get("technology")))
        rel_lines.append(_puml_node(
            "Rel", f'{_puml_alias("c_", dependency["sourceId"])}, {target_alias}',
            dependency.get("description") or "", dependency.get("technology"),
        ))
    if node_lines:
        lines.append("")
        lines.extend(node_lines)
    if rel_lines:
        lines.append("")
        lines.extend(rel_lines)
    lines.append("")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def _generate_batch(
    project: Any, generator_name: str, model: str, source_url: str, batch: list[str], invoker: Callable[..., Any],
    tenant: str | None = None,
) -> dict[str, Any]:
    payload = {"sourceUrl": source_url, "sourceFiles": batch, **({"tenant": tenant} if tenant else {})}
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return validate_catalog(invoker(project, generator_name, model, payload, max_attempts=1))
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _publisher_payload(request: dict[str, Any], catalog: dict[str, Any], puml: str) -> dict[str, Any]:
    _, repository, _, _ = parse_source_url(request["sourceUrl"])
    directory = request.get("targetDirectory", f"{repository}/service-dependencies").strip("/")
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "catalogs": [{
            "sourceUrl": request["sourceUrl"],
            "catalog": catalog,
            "targetPath": f"{directory}/service-dependencies.json",
            "puml": puml,
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
    generator_batch_size: int = 12,
    max_concurrency: int = 8,
    invoker: Callable[..., Any] = invoke_agent,
) -> dict[str, Any]:
    request = parse_workflow_request(json.dumps(request))
    source_url = request["sourceUrl"]
    batch_errors: list[dict[str, Any]] = []
    discovered: dict[str, Any] | None = None
    try:
        discovery_request = {"sourceUrl": source_url, **({"tenant": request["tenant"]} if request.get("tenant") else {})}
        discovered = validate_discovery_output(invoker(project, discovery_name, model, discovery_request), source_url, max_files)
        files = discovered["sourceFiles"]
        owner, repository, ref, path = parse_source_url(source_url)
        if files:
            batches = [files[index:index + max(1, generator_batch_size)] for index in range(0, len(files), max(1, generator_batch_size))]
            catalogs = []
            # Each batch is an independent, blocking LLM call -- run them concurrently (bounded) instead
            # of one at a time, since the deployment's throughput quota is far above what one repo needs.
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(max_concurrency, len(batches)))) as executor:
                future_to_batch = {
                    executor.submit(_generate_batch, project, generator_name, model, source_url, batch, invoker, request.get("tenant")): batch
                    for batch in batches
                }
                for future in concurrent.futures.as_completed(future_to_batch):
                    batch = future_to_batch[future]
                    try:
                        catalogs.append(future.result())
                    except Exception as error:
                        batch_errors.append({"files": batch, "errorType": type(error).__name__, "message": str(error)[:300]})
            if batch_errors:
                raise WorkflowError(
                    "partial_generation_failed",
                    f"{len(batch_errors)} of {len(batches)} service-dependency batches failed; refusing to publish a partial catalog.",
                )
            catalog = merge_catalogs(catalogs)
            if (catalog["repository"], catalog["ref"], catalog["path"]) != (f"{owner}/{repository}", ref, path):
                raise WorkflowError("source_identity_mismatch", "Generated catalog source identity does not match sourceUrl.")
        else:
            # No candidate files means no outbound HTTP/gRPC dependencies exist to evidence -- a
            # legitimate, stable result, not a failure. Report an explicit empty catalog so the
            # manifest's commit hash advances instead of rescanning this repository forever.
            catalog = {
                "repository": f"{owner}/{repository}", "ref": ref, "path": path,
                "systemName": repository, "containers": [], "dependencies": [],
            }
    except Exception as error:
        return {
            "success": False,
            "sourceUrl": source_url,
            "generatedCatalogCount": 0,
            "discoveredFileCount": len(discovered["sourceFiles"]) if discovered else 0,
            "excludedFileCount": len(discovered["excludedFiles"]) if discovered else 0,
            "generationErrors": batch_errors or [{"errorType": type(error).__name__, "message": str(error)[:300]}],
            "catalogs": [],
            "pullRequest": None,
            "errors": [{"code": getattr(error, "code", "generation_failed"), "message": str(error)[:300]}],
        }
    puml = catalog_to_puml(catalog)
    catalog_items = [{"sourceUrl": source_url, "catalog": catalog, "puml": puml}]
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
    publication = invoker(project, publisher_name, model, _publisher_payload(request, catalog, puml), max_attempts=1)
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {"success": publication["success"], **common, "pullRequest": publication}
