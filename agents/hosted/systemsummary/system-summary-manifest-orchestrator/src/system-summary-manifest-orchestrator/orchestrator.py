"""Manifest-driven system-summary generation for the service catalogue itself.

Unlike the dbschema/eventcatalog/service-dependency orchestrators, this agent never scans a target
repository's raw source. It reads the manifest already published in the tenant's data repository,
fetches each listed repository's already-published catalogs from that same repository, and asks
system-summary-generator to summarize each one. The combined result is published back into the same
repository as one file.
"""

from __future__ import annotations

import functools
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import github_app


# Without a token GitHub allows 60 API requests an hour per IP, which one run (a contents listing
# per repository) exhausts; with one it is 5,000. Same connection the other orchestrators use to
# read the catalogue repository.
RAW_PREFIX = "https://raw.githubusercontent.com/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 50
OPENAPI_SUFFIX = ".openapi.json"
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


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

    @property
    def tenant(self) -> str:
        """The data repository holding the manifest identifies the tenant."""
        return f"{self.owner}/{self.repository}"


def parse_request(input_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(input_text)
    except json.JSONDecodeError as error:
        raise ManifestError("invalid_json", "Input must be one JSON object.") from error
    if not isinstance(payload, dict) or not set(payload).issubset({"sourceUrl", "deferPublication"}) or "sourceUrl" not in payload:
        raise ManifestError("invalid_input", 'Input must contain "sourceUrl" and optionally "deferPublication".')
    source_url = payload["sourceUrl"]
    if not isinstance(source_url, str) or not source_url.strip():
        raise ManifestError("invalid_input", "sourceUrl must be a non-empty string.")
    parse_blob_url(source_url.strip())
    defer = payload.get("deferPublication", False)
    if not isinstance(defer, bool):
        raise ManifestError("invalid_input", "deferPublication must be a boolean.")
    return {"sourceUrl": source_url.strip(), "deferPublication": defer}


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
            "invalid_source_url", "sourceUrl must match https://github.com/owner/repository/blob/ref/path.json."
        )
    owner, repository, _, ref, *path_parts = parts
    path = "/".join(path_parts)
    if not path.endswith(".json"):
        raise ManifestError("invalid_source_url", "sourceUrl must reference a JSON file.")
    return GitHubBlob(owner, repository.removesuffix(".git"), ref, path)


def _fetch_json(url: str, token: str | None = None) -> Any | None:
    """Fetch and parse JSON from url. Returns None on a 404 (the file or directory does not exist)."""
    headers = {"User-Agent": "system-summary-manifest-orchestrator", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        if url.startswith(RAW_PREFIX):
            # The contents API accepts App installation tokens; raw.githubusercontent.com is for anonymous reads.
            # _raw_url quotes the ref, so the first three segments are always owner, repository and ref.
            owner, repository, ref, path = url[len(RAW_PREFIX):].split("/", 3)
            url = f"https://api.github.com/repos/{owner}/{repository}/contents/{path}?ref={ref}"
            headers["Accept"] = "application/vnd.github.raw"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ManifestError("github_api_error", f"GitHub returned HTTP {error.code} for {url}.") from error
    except urllib.error.URLError as error:
        raise ManifestError("github_unavailable", "GitHub could not be reached.") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise ManifestError("response_too_large", f"Response from {url} exceeded the size limit.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("invalid_response", f"{url} did not return valid UTF-8 JSON.") from error


def _raw_url(blob: GitHubBlob, path: str) -> str:
    return (
        f"{RAW_PREFIX}{urllib.parse.quote(blob.owner, safe='')}/"
        f"{urllib.parse.quote(blob.repository, safe='')}/{urllib.parse.quote(blob.ref, safe='')}/"
        f"{urllib.parse.quote(path, safe='/')}"
    )


def download_manifest(blob: GitHubBlob, fetch: Callable[[str], Any] = _fetch_json) -> object:
    manifest = fetch(_raw_url(blob, blob.path))
    if manifest is None:
        raise ManifestError("invalid_manifest", "The manifest could not be found.")
    return manifest


def repo_slug(repository_url: str) -> str:
    return repository_url.strip().rstrip("/").split("/")[-1].removesuffix(".git")


def repo_owner_and_name(repository_url: str) -> str:
    parts = [part for part in urllib.parse.urlparse(repository_url.strip()).path.split("/") if part]
    if len(parts) < 2:
        raise ManifestError("invalid_manifest_entry", f"github-repo is not a valid repository URL: {repository_url!r}.")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def build_generator_input(entry: dict[str, Any], blob: GitHubBlob, fetch: Callable[[str], Any]) -> dict[str, Any]:
    repository_url = entry.get("github-repo")
    if not isinstance(repository_url, str) or not repository_url.strip():
        raise ManifestError("invalid_manifest_entry", "Each manifest entry must contain a non-empty github-repo.")
    slug = repo_slug(repository_url)

    def catalog(node: str, target_path: str) -> dict[str, Any] | None:
        if not entry.get(node):
            return None
        value = fetch(_raw_url(blob, f"{slug}/{target_path}"))
        return value if isinstance(value, dict) else None

    database = catalog("dbschema", "db-schema/database.schema.json")
    events = catalog("eventcatalog", "event-catalog/events-and-commands.json")
    dependencies = catalog("service-dependencies", "service-dependencies/service-dependencies.json")

    controllers: list[str] = []
    if entry.get("specs"):
        listing = fetch(
            f"https://api.github.com/repos/{urllib.parse.quote(blob.owner, safe='')}/"
            f"{urllib.parse.quote(blob.repository, safe='')}/contents/"
            f"{urllib.parse.quote(slug, safe='')}/open-api?ref={urllib.parse.quote(blob.ref, safe='')}"
        )
        if isinstance(listing, list):
            controllers = sorted(
                item["name"][: -len(OPENAPI_SUFFIX)]
                for item in listing
                if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].endswith(OPENAPI_SUFFIX)
            )

    return {
        "repository": repo_owner_and_name(repository_url),
        "database": database,
        "events": events,
        "dependencies": dependencies,
        "apiControllers": controllers,
    }


def _parse_json_value(text: str) -> Any:
    value = text.strip()
    fence = FENCE_RE.fullmatch(value)
    if fence:
        value = fence.group(1).strip()
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


def invoke_agent(project: Any, agent_name: str, model: str, payload: dict[str, Any], max_attempts: int = 2) -> Any:
    last_error: Exception | None = None
    for _ in range(max(1, max_attempts)):
        try:
            client = project.get_openai_client(agent_name=agent_name)
            response = client.responses.create(model=model, input=json.dumps(payload, separators=(",", ":")), timeout=180)
            return _parse_json_value(response.output_text)
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _validate_summary_output(value: Any, repository: str) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        error = value["error"]
        raise ManifestError(
            str(error.get("code", "generation_failed")), str(error.get("message", "System summary generation failed."))
        )
    if not isinstance(value, dict) or set(value) != {"repository", "name", "description", "domain", "capabilities", "confidence"}:
        raise ManifestError(
            "invalid_generator_output",
            "Generator must return repository, name, description, domain, capabilities, and confidence.",
        )
    if value["repository"] != repository:
        raise ManifestError("invalid_generator_output", "Generator returned a summary for another repository.")
    return value


def run_manifest(
    project: Any,
    request: dict[str, Any],
    generator_name: str,
    publisher_name: str,
    model: str,
    max_entries: int = MAX_ENTRIES,
    workflow_concurrency: int = 4,
    fetch: Callable[[str], Any] | None = None,
    invoker: Callable[..., Any] = invoke_agent,
) -> dict[str, Any]:
    blob = parse_blob_url(request["sourceUrl"])
    # The manifest's repository is the tenant: every file this run reads is in it, so one read token covers the run.
    fetch = fetch or functools.partial(_fetch_json, token=github_app.read_token(blob.tenant))
    manifest = download_manifest(blob, fetch)
    if not isinstance(manifest, list) or not manifest:
        raise ManifestError("invalid_manifest", "The manifest must be a non-empty array.")
    if len(manifest) > max_entries:
        raise ManifestError("too_many_entries", f"A run may summarize at most {max_entries} repositories.")

    def summarize(index: int, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        repository_label = entry.get("github-repo") or f"entry[{index}]"
        try:
            generator_input = build_generator_input(entry, blob, fetch)
            repository_label = generator_input["repository"]
            # The tenant lets the generator record its model usage against the right data repository.
            generated = invoker(project, generator_name, model, {**generator_input, "tenant": blob.tenant})
            return _validate_summary_output(generated, generator_input["repository"]), None
        except Exception as error:
            return None, {
                "repository": repository_label,
                "stage": "system_summary",
                "errorType": type(error).__name__,
                "message": str(error)[:300],
            }

    with ThreadPoolExecutor(max_workers=max(1, workflow_concurrency)) as executor:
        futures = [
            executor.submit(summarize, index, entry) if isinstance(entry, dict) else None
            for index, entry in enumerate(manifest)
        ]
    # Process results in manifest order so the systems list and failures do not depend on completion order.
    systems: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, future in enumerate(futures):
        if future is None:
            failures.append(
                {"repository": f"entry[{index}]", "stage": "manifest_entry", "errorType": "ManifestError", "message": "Manifest entry must be an object."}
            )
            continue
        system, failure = future.result()
        if system is not None:
            systems.append(system)
        else:
            failures.append(failure)

    if not systems:
        return {
            "success": False,
            "status": "failed",
            "sourceUrl": request["sourceUrl"],
            "checkedCount": len(manifest),
            "generatedSystemCount": 0,
            "systems": [],
            "failures": failures,
            "pullRequest": None,
        }

    if request.get("deferPublication", False):
        return {
            "success": not failures,
            "status": "generated" if not failures else "partial",
            "sourceUrl": request["sourceUrl"],
            "checkedCount": len(manifest),
            "generatedSystemCount": len(systems),
            "systems": systems,
            "failures": failures,
            "pullRequest": None,
        }

    publication = invoker(
        project,
        publisher_name,
        model,
        {
            "repository": f"{blob.owner}/{blob.repository}",
            "targetPath": "system-summaries.json",
            "fileContent": {"systems": systems},
            "pullRequestTitle": "Update generated system summaries",
            "pullRequestBody": "Generated system summaries for the repositories in the manifest.",
        },
        max_attempts=1,
    )
    if not isinstance(publication, dict) or not isinstance(publication.get("success"), bool):
        raise ManifestError("invalid_publisher_output", "PR creator response does not match its JSON contract.")
    return {
        "success": publication["success"] and not failures,
        "status": "created" if publication["success"] else "failed",
        "sourceUrl": request["sourceUrl"],
        "checkedCount": len(manifest),
        "generatedSystemCount": len(systems),
        "systems": systems,
        "failures": failures,
        "pullRequest": publication,
    }
