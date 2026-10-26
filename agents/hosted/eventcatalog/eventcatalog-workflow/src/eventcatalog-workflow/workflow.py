"""Event and command discovery, generation, and publication orchestration."""

from __future__ import annotations

import copy
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
        "sourceUrl", "targetRepository", "targetDirectory", "targetBaseBranch",
        "branchName", "pullRequestTitle", "pullRequestBody", "deferPublication",
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
    if not isinstance(value, dict) or set(value) != {"repository", "ref", "path", "commands", "events"}:
        raise WorkflowError("invalid_generator_output", "Generator must return repository, ref, path, commands, and events.")
    if any(not isinstance(value[field], str) for field in ("repository", "ref", "path")):
        raise WorkflowError("invalid_generator_output", "Generator returned invalid source identity fields.")
    if not isinstance(value["commands"], list) or not isinstance(value["events"], list):
        raise WorkflowError("invalid_generator_output", "Generator returned invalid command or event arrays.")
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


def merge_catalogs(catalogs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []

    def source_stem(message: dict[str, Any]) -> str:
        source_file = str(message.get("sourceFile") or "")
        return source_file.rsplit("/", 1)[-1].rsplit(".", 1)[0].strip().lower()

    def is_declaration_source(message: dict[str, Any]) -> bool:
        name = str(message.get("name") or "").lower()
        declaration_names = {name}
        for suffix in ("command", "event"):
            if name.endswith(suffix):
                declaration_names.add(name.removesuffix(suffix))
        return source_stem(message) in declaration_names

    first = catalogs[0]
    result = {"repository": first["repository"], "ref": first["ref"], "path": first["path"], "commands": [], "events": []}
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for catalog in catalogs:
        if (catalog["repository"], catalog["ref"], catalog["path"]) != (result["repository"], result["ref"], result["path"]):
            raise WorkflowError("source_identity_mismatch", "Generator batches returned different source identities.")
        for kind in ("commands", "events"):
            for message in catalog[kind]:
                identity = (kind, str(message.get("namespace") or ""), str(message.get("name") or ""))
                existing = seen.get(identity)
                if existing is None:
                    copied = copy.deepcopy(message)
                    seen[identity] = copied
                    result[kind].append(copied)
                    continue
                existing_is_declaration = is_declaration_source(existing)
                candidate_is_declaration = is_declaration_source(message)
                if candidate_is_declaration and not existing_is_declaration:
                    existing["sourceFile"] = message["sourceFile"]
                    existing["description"] = message.get("description")
                    existing["fields"] = copy.deepcopy(message.get("fields", []))
                    existing_is_declaration = True
                else:
                    existing["description"] = existing.get("description") or message.get("description")
                existing_source = str(existing.get("sourceFile") or "")
                candidate_source = str(message.get("sourceFile") or "")
                if candidate_is_declaration:
                    if existing_is_declaration and candidate_source != existing_source:
                        warnings.append(
                            {
                                "errorType": "ConflictingSourceFile",
                                "message": (
                                    f"Generator returned conflicting source files for {identity[2]!r} "
                                    f"({existing_source!r} vs {candidate_source!r}); kept the first occurrence."
                                ),
                            }
                        )
                    else:
                        existing["sourceFile"] = candidate_source
                elif not existing_is_declaration:
                    existing["sourceFile"] = min(existing_source, candidate_source)
                fields = {field["name"]: field for field in existing.get("fields", [])}
                for field in message.get("fields", []):
                    if field["name"] in fields and fields[field["name"]] != field:
                        # existing already holds the declaration's value here (the wholesale copy above
                        # applies before this loop runs); keep it and warn instead of failing the batch.
                        warnings.append(
                            {
                                "errorType": "ConflictingField",
                                "message": (
                                    f"Generator returned conflicting field {field['name']!r} for {identity[2]!r}; "
                                    "kept the declaration's value (or the first occurrence) and discarded the other."
                                ),
                            }
                        )
                        continue
                    if field["name"] not in fields:
                        existing["fields"].append(copy.deepcopy(field))
                        fields[field["name"]] = field
                handlers = {(handler["name"], handler["sourceFile"]) for handler in existing.get("handlers", [])}
                for handler in message.get("handlers", []):
                    key = (handler["name"], handler["sourceFile"])
                    if key not in handlers:
                        existing["handlers"].append(copy.deepcopy(handler))
                        handlers.add(key)
                senders = existing.setdefault("senders", [])
                for sender in message.get("senders", []):
                    if not any((seen["name"], seen["sourceFile"]) == (sender["name"], sender["sourceFile"]) for seen in senders):
                        senders.append(copy.deepcopy(sender))
                existing["fields"].sort(key=lambda item: item["name"])
                existing["handlers"].sort(key=lambda item: (item["name"], item["sourceFile"]))
                senders.sort(key=lambda item: (item["sourceFile"], item["name"]))
    _fold_sent_only_messages(result, warnings)
    for kind in ("commands", "events"):
        result[kind].sort(key=lambda item: ((item.get("namespace") or ""), item.get("name") or "", item.get("sourceFile") or ""))
    return result, warnings


def _fold_sent_only_messages(catalog: dict[str, Any], warnings: list[dict[str, Any]]) -> None:
    """Fold a message recorded only because it is sent into the declared message of the same name.

    The generator guesses a sent type's namespace from the file's imports; when another batch declares the same
    name under a different namespace (usually the repository's own), the declaration is right.
    """
    for kind in ("commands", "events"):
        messages = catalog[kind]
        sent_only = lambda m: not m.get("handlers") and not m.get("fields") and m.get("senders") and \
            m.get("sourceFile") in {sender["sourceFile"] for sender in m["senders"]}
        for message in [m for m in messages if sent_only(m)]:
            declared = [m for m in messages if m is not message and m.get("name") == message.get("name") and not sent_only(m)]
            if len(declared) != 1:
                continue
            target = declared[0]
            senders = target.setdefault("senders", [])
            for sender in message["senders"]:
                if sender not in senders:
                    senders.append(sender)
            senders.sort(key=lambda item: (item["sourceFile"], item["name"]))
            messages.remove(message)
            warnings.append({"errorType": "FoldedSentMessage",
                             "message": f"Recorded senders of {message['name']!r} on its declaration in {target.get('namespace')!r}."})


def _is_context_overflow(error: Exception) -> bool:
    message = str(error)
    return "context_length_exceeded" in message or "exceeds the context window" in message


def _generate_batch(
    project: Any,
    generator_name: str,
    model: str,
    source_url: str,
    batch: list[str],
    invoker: Callable[..., Any],
    max_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns the batch's catalogs and any files skipped as too large for the model.

    A batch the model cannot fit is split in half and each half generated on its own (not retried as is,
    since it would overflow again). A single file that still overflows is skipped with a warning rather
    than failing the whole repository; such files are generated or vendored code, not message declarations.
    """
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        if attempt:
            # Back off before retrying so concurrent batches do not hammer a throttled model deployment.
            time.sleep(retry_delay_seconds * 2 ** (attempt - 1))
        try:
            return [validate_catalog(invoker(project, generator_name, model, {"sourceUrl": source_url, "sourceFiles": batch}, max_attempts=1))], []
        except Exception as error:
            if _is_context_overflow(error):
                break
            last_error = error
    else:
        assert last_error is not None
        raise last_error
    if len(batch) == 1:
        return [], [{"files": batch, "errorType": "SourceFileTooLarge", "message": "Skipped: the file alone exceeds the model's context window."}]
    middle = len(batch) // 2
    catalogs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for half in (batch[:middle], batch[middle:]):
        half_catalogs, half_skipped = _generate_batch(
            project, generator_name, model, source_url, half, invoker, max_attempts, retry_delay_seconds,
        )
        catalogs.extend(half_catalogs)
        skipped.extend(half_skipped)
    return catalogs, skipped


def _publisher_payload(request: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    _, repository, _, _ = parse_source_url(request["sourceUrl"])
    directory = request.get("targetDirectory", f"{repository}/event-catalog").strip("/")
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "catalogs": [{"sourceUrl": request["sourceUrl"], "catalog": catalog, "targetPath": f"{directory}/events-and-commands.json"}],
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
    max_files: int = 100,
    generator_batch_size: int = 10,
    generator_concurrency: int = 3,
    invoker: Callable[..., Any] = invoke_agent,
    retry_delay_seconds: float = 5.0,
) -> dict[str, Any]:
    request = parse_workflow_request(json.dumps(request))
    source_url = request["sourceUrl"]
    batch_errors: list[dict[str, Any]] = []
    try:
        discovery_request = {"sourceUrl": source_url, **({"tenant": request["tenant"]} if request.get("tenant") else {})}
        discovered = validate_discovery_output(invoker(project, discovery_name, model, discovery_request), source_url, max_files)
        files = discovered["sourceFiles"]
        owner, repository, ref, path = parse_source_url(source_url)
        if not files:
            # No candidate message/handler files is a legitimate outcome (e.g. a repository with no
            # pub/sub activity at all), not a failure -- matching how the other pipelines in this repo
            # treat "nothing evidenced" as a valid, empty result rather than fail-closed.
            catalog = {"repository": f"{owner}/{repository}", "ref": ref, "path": path, "commands": [], "events": []}
        else:
            batches = [files[index:index + max(1, generator_batch_size)] for index in range(0, len(files), max(1, generator_batch_size))]
            with ThreadPoolExecutor(max_workers=max(1, generator_concurrency)) as executor:
                futures = [
                    executor.submit(
                        _generate_batch, project, generator_name, model, source_url, batch, invoker,
                        retry_delay_seconds=retry_delay_seconds,
                    )
                    for batch in batches
                ]
            # Collect in batch order, not completion order, so merge_catalogs keeps the same first occurrence.
            catalogs = []
            skipped_files: list[dict[str, Any]] = []
            for batch, future in zip(batches, futures):
                try:
                    batch_catalogs, batch_skipped = future.result()
                    catalogs.extend(batch_catalogs)
                    skipped_files.extend(batch_skipped)
                except Exception as error:
                    batch_errors.append({"files": batch, "errorType": type(error).__name__, "message": str(error)[:300]})
            if batch_errors:
                raise WorkflowError(
                    "partial_generation_failed",
                    f"{len(batch_errors)} of {len(batches)} event catalog batches failed; refusing to publish a partial catalog.",
                )
            if not catalogs:
                raise WorkflowError("generation_failed", "No source batch produced a valid event and command catalog.")
            catalog, merge_warnings = merge_catalogs(catalogs)
            batch_errors.extend(skipped_files)
            batch_errors.extend(merge_warnings)
            # An empty result here (files existed but none declared an event or command) is also
            # valid -- same reasoning as the empty-discovery case above.
            if (catalog["repository"], catalog["ref"], catalog["path"]) != (f"{owner}/{repository}", ref, path):
                raise WorkflowError("source_identity_mismatch", "Generated catalog source identity does not match sourceUrl.")
    except Exception as error:
        return {
            "success": False, "sourceUrl": source_url, "generatedCatalogCount": 0,
            "discoveredFileCount": 0, "excludedFileCount": 0,
            "generationErrors": batch_errors or [{"errorType": type(error).__name__, "message": str(error)[:300]}],
            "catalogs": [], "pullRequest": None,
            "errors": [{"code": getattr(error, "code", "generation_failed"), "message": str(error)[:300]}],
        }
    catalog_items = [{"sourceUrl": source_url, "catalog": catalog}]
    common = {
        "sourceUrl": source_url, "generatedCatalogCount": 1,
        "discoveredFileCount": len(discovered["sourceFiles"]), "excludedFileCount": len(discovered["excludedFiles"]),
        "generationErrors": batch_errors, "catalogs": catalog_items, "errors": [],
    }
    if request.get("deferPublication", False):
        return {"success": True, **common, "pullRequest": None}
    publication = invoker(project, publisher_name, model, _publisher_payload(request, catalog), max_attempts=1)
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {"success": publication["success"], **common, "pullRequest": publication}
