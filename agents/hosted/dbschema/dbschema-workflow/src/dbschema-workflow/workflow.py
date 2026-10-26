"""Database-schema generation and publication orchestration."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
        try:
            result, _ = json.JSONDecoder().raw_decode(value[min(starts) :])
            return result
        except json.JSONDecodeError as error:
            raise WorkflowError("invalid_agent_response", "Agent response was not JSON.") from error


def parse_source_url(value: object) -> tuple[str, str, str]:
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


def validate_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise WorkflowError(
            str(error.get("code", "schema_generation_failed")),
            str(error.get("message", "Database schema generation failed.")),
        )
    if not isinstance(value, dict) or set(value) != {"database", "tables", "types"}:
        raise WorkflowError("invalid_generator_output", "Generator response must contain database, tables, and types.")
    if not isinstance(value["database"], dict) or set(value["database"]) != {"name", "engine"}:
        raise WorkflowError("invalid_generator_output", "Generator response contains an invalid database object.")
    if not isinstance(value["tables"], list) or not isinstance(value["types"], list):
        raise WorkflowError("invalid_generator_output", "Generator response contains an incomplete database schema.")
    return value


def _validate_blob_url(value: object, source: tuple[str, str, str]) -> str:
    if not isinstance(value, str):
        raise WorkflowError("invalid_discovery_output", "Discovered file URLs must be strings.")
    parsed = urllib.parse.urlparse(value)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or len(parts) < 5 or parts[2] != "blob"
        or (parts[0], parts[1].removesuffix(".git"), parts[3]) != source
        or any(part in {".", ".."} or "/" in part or "\\" in part for part in parts)
    ):
        raise WorkflowError("invalid_discovery_output", "Discovered files must be GitHub blob URLs from the source repository and ref.")
    return value


def validate_discovery_output(value: Any, source_url: str, max_files: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schemaFiles", "excludedFiles"}:
        raise WorkflowError("invalid_discovery_output", "Discovery must contain exactly schemaFiles and excludedFiles.")
    files = value["schemaFiles"]
    excluded = value["excludedFiles"]
    # An empty list is valid: discovery found no evidence that the source persists anything.
    if not isinstance(files, list) or len(files) > max_files:
        raise WorkflowError("invalid_discovery_output", f"schemaFiles must contain at most {max_files} files.")
    source = parse_source_url(source_url)
    validated = [_validate_blob_url(item, source) for item in files]
    if len(validated) != len(set(validated)):
        raise WorkflowError("invalid_discovery_output", "schemaFiles must be unique.")
    if not isinstance(excluded, list) or any(not isinstance(item, dict) or set(item) != {"path", "reason"} or not all(isinstance(item[key], str) and item[key] for key in item) for item in excluded):
        raise WorkflowError("invalid_discovery_output", "excludedFiles has an invalid shape.")
    return {"schemaFiles": validated, "excludedFiles": excluded}


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


def merge_schemas(schemas: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    database_name: str | None = None
    database_engine: str | None = None
    tables: list[dict[str, Any]] = []
    table_identities: set[tuple[Any, Any]] = set()
    types: list[dict[str, Any]] = []
    type_names: set[str] = set()
    warnings: list[dict[str, Any]] = []
    for schema in schemas:
        database_name = database_name or schema["database"]["name"]
        database_engine = database_engine or schema["database"]["engine"]
        for table in schema["tables"]:
            identity = (table["schema"], table["name"])
            if identity in table_identities:
                warnings.append(
                    {
                        "errorType": "DuplicateTable",
                        "message": (
                            f"Generator produced table {table['name']!r} more than once; "
                            "kept the first occurrence and discarded the duplicate."
                        ),
                    }
                )
                continue
            table_identities.add(identity)
            tables.append(table)
        for named_type in schema["types"]:
            if named_type["name"] not in type_names:
                type_names.add(named_type["name"])
                types.append(named_type)
    tables = _merge_entity_tables(tables, warnings)
    return {"database": {"name": database_name, "engine": database_engine}, "tables": tables, "types": types}, warnings


def _merge_entity_tables(tables: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold ORM entity classes into the SQL table they map to.

    A repository with both a SQL database project and entity classes describes each table twice: once from the DDL
    (with a schema such as dbo and SQL types) and once from the entity (no schema, C# types), usually in different
    batches. When a name has exactly one table with a schema, entity copies without one are merged into it: the SQL
    columns are kept, the PII flag is set if either copy has it, and relationships and indexes are combined.
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        by_name.setdefault(str(table["name"]).lower(), []).append(table)
    folded: set[int] = set()
    for group in by_name.values():
        with_schema = [table for table in group if table.get("schema")]
        entities = [table for table in group if not table.get("schema")]
        if len(with_schema) != 1 or not entities:
            continue   # nothing to fold, or ambiguous (several schemas)
        target = with_schema[0]
        for entity in entities:
            target["hasPii"] = bool(target.get("hasPii") or entity.get("hasPii"))
            target["entity"] = target.get("entity") or entity.get("entity")
            if not target.get("columns"):
                target["columns"] = entity.get("columns", [])
            target.setdefault("relationships", [])
            target.setdefault("indexes", [])
            seen = {(str(r["targetTable"]).lower(), tuple(c.lower() for c in r["fromColumns"])) for r in target["relationships"]}
            for relationship in entity.get("relationships", []):
                key = (str(relationship["targetTable"]).lower(), tuple(c.lower() for c in relationship["fromColumns"]))
                if key not in seen:
                    seen.add(key)
                    target["relationships"].append(relationship)
            index_names = {str(index["name"]).lower() for index in target["indexes"]}
            target["indexes"] += [index for index in entity.get("indexes", []) if str(index["name"]).lower() not in index_names]
            folded.add(id(entity))
            warnings.append({
                "errorType": "MergedEntityTable",
                "message": f"Merged entity {entity.get('entity') or entity['name']!r} into table {target['schema']}.{target['name']}.",
            })
    return [table for table in tables if id(table) not in folded]


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
    payload = {"sourceUrl": source_url, "sourceFiles": batch, **({"tenant": tenant} if tenant else {})}
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        if attempt:
            # Back off before retrying so concurrent batches do not hammer a throttled model deployment.
            time.sleep(retry_delay_seconds * 2 ** (attempt - 1))
        try:
            return validate_schema(
                invoker(project, generator_name, model, payload, max_attempts=1)
            )
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _publisher_payload(request: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    _, source_repository, _ = parse_source_url(request["sourceUrl"])
    target_directory = request.get("targetDirectory", f"{source_repository}/db-schema")
    payload: dict[str, Any] = {
        "repository": request["targetRepository"],
        "schemas": [
            {
                "sourceUrl": request["sourceUrl"],
                "schema": schema,
                "targetPath": f"{target_directory.strip('/')}/database.schema.json",
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


class _NoDatabase(Exception):
    pass


def run_workflow(
    project: Any,
    request: dict[str, Any],
    discovery_name: str,
    generator_name: str,
    publisher_name: str,
    model: str,
    max_files: int = 100,
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
        discovered = validate_discovery_output(discovery, source_url, max_files)
        schema_files = discovered["schemaFiles"]
        if not schema_files:
            raise _NoDatabase()
        batches = [
            schema_files[index : index + generator_batch_size]
            for index in range(0, len(schema_files), generator_batch_size)
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
        # Collect in batch order, not completion order, so merge_schemas keeps the same first occurrence.
        batch_schemas = []
        for batch, future in zip(batches, futures):
            try:
                batch_schemas.append(future.result())
            except Exception as error:
                batch_errors.append(
                    {
                        "sourceUrl": source_url,
                        "files": batch,
                        "errorType": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )
        if not batch_schemas:
            raise WorkflowError("generation_failed", "No batch of source files produced a valid schema.")
        schema, merge_warnings = merge_schemas(batch_schemas)
        batch_errors.extend(merge_warnings)
    except _NoDatabase:
        # Nothing persisted, so the database is empty rather than something for the model to invent.
        schema = {"database": {"name": None, "engine": None}, "tables": [], "types": []}
    except Exception as error:
        return {
            "success": False,
            "sourceUrl": source_url,
            "generatedSchemaCount": 0,
            "discoveredFileCount": 0,
            "excludedFileCount": 0,
            "generationErrors": batch_errors
            or [{"sourceUrl": source_url, "errorType": type(error).__name__, "message": str(error)[:300]}],
            "schemas": [],
            "pullRequest": None,
            "errors": [{"code": "generation_failed", "message": "The database schema was not generated."}],
        }

    schemas = [{"sourceUrl": source_url, "schema": schema}]
    if request.get("deferPublication", False):
        return {
            "success": True,
            "sourceUrl": source_url,
            "generatedSchemaCount": 1,
            "discoveredFileCount": len(discovered["schemaFiles"]),
            "excludedFileCount": len(discovered["excludedFiles"]),
            "generationErrors": batch_errors,
            "schemas": schemas,
            "pullRequest": None,
            "errors": [],
        }

    publication = invoker(
        project,
        publisher_name,
        model,
        _publisher_payload(request, schema),
        max_attempts=1,
    )
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise WorkflowError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {
        "success": publication["success"],
        "sourceUrl": source_url,
        "generatedSchemaCount": 1,
        "discoveredFileCount": len(discovered["schemaFiles"]),
        "excludedFileCount": len(discovered["excludedFiles"]),
        "generationErrors": batch_errors,
        "schemas": schemas,
        "pullRequest": publication,
        "errors": [],
    }
