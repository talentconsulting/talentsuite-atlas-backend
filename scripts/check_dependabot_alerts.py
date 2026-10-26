"""Check GitHub Dependabot alerts for every repository in the shared manifest
and publish one JSON snapshot per repository through a single pull request.

Unlike the hosted Foundry pipelines under agents/hosted/, this reads
structured data straight from the GitHub API, so no LLM agent is involved.

Required environment variables:
    DEPENDABOT_ALERTS_TOKEN  Token with "Dependabot alerts: Read-only" access
                             (or classic PAT with the security_events scope)
                             to every repository listed in the manifest.
    GITHUB_PR_TOKEN          Token with contents + pull-requests write access
                             to the tenant's data repository holding the manifest.

Optional environment variables:
    MANIFEST_SOURCE_URL      The tenant's manifest. Defaults to talentsuite-atlas (DfE);
                             the workflow sets it for each repository in ATLAS_DATA_REPOSITORIES.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

DEFAULT_MANIFEST_SOURCE_URL = "https://github.com/talentconsulting/talentsuite-atlas/blob/main/manifest.json"
API_URL = "https://api.github.com"
ALERTS_PER_PAGE = 100


class CheckError(RuntimeError):
    pass


def _api_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "check-dependabot-alerts",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return (json.loads(raw) if raw else {}), dict(response.headers.items())
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = str(json.loads(error.read().decode("utf-8")).get("message", ""))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        raise CheckError(f"GitHub API returned HTTP {error.code} for {url}.{(' ' + detail) if detail else ''}") from error
    except urllib.error.URLError as error:
        raise CheckError(f"GitHub API could not be reached: {error}") from error


def parse_manifest_source_url(source_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(source_url)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if parsed.hostname not in {"github.com", "www.github.com"} or len(parts) < 5 or parts[2] != "blob":
        raise CheckError("MANIFEST_SOURCE_URL must match https://github.com/owner/repository/blob/ref/path.json.")
    owner, repository, _, ref, *path_parts = parts
    return f"{owner}/{repository.removesuffix('.git')}", "/".join(path_parts)


def fetch_manifest(source_url: str) -> list[dict[str, Any]]:
    repository, path = parse_manifest_source_url(source_url)
    ref = urllib.parse.urlparse(source_url).path.split("/")[4]
    raw_url = f"https://raw.githubusercontent.com/{repository}/{ref}/{urllib.parse.quote(path, safe='/')}"
    request = urllib.request.Request(raw_url, headers={"User-Agent": "check-dependabot-alerts"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            manifest = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckError(f"Could not read manifest from {raw_url}: {error}") from error
    if not isinstance(manifest, list):
        raise CheckError("Manifest root must be an array.")
    return manifest


def manifest_repositories(manifest: list[dict[str, Any]]) -> list[str]:
    repositories = []
    for index, entry in enumerate(manifest):
        github_repo = entry.get("github-repo") if isinstance(entry, dict) else None
        if not isinstance(github_repo, str):
            raise CheckError(f"Manifest entry {index} is missing github-repo.")
        parts = [part for part in urllib.parse.urlparse(github_repo).path.split("/") if part]
        if len(parts) != 2:
            raise CheckError(f"Manifest entry {index}.github-repo must be https://github.com/owner/repository.")
        repositories.append(f"{parts[0]}/{parts[1].removesuffix('.git')}")
    return repositories


def _next_link(headers: dict[str, str]) -> str | None:
    link_header = next((value for key, value in headers.items() if key.lower() == "link"), None)
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' not in segment:
            continue
        start, end = segment.find("<"), segment.find(">")
        if start != -1 and end != -1:
            return segment[start + 1:end]
    return None


def fetch_open_alerts(repository: str, token: str) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    url = f"{API_URL}/repos/{repository}/dependabot/alerts?state=open&per_page={ALERTS_PER_PAGE}"
    while url:
        data, headers = _api_request("GET", url, token)
        if not isinstance(data, list):
            raise CheckError(f"Unexpected Dependabot alerts response for {repository}.")
        alerts.extend(data)
        url = _next_link(headers)
    return alerts


def summarize_alerts(repository: str, alerts: list[dict[str, Any]]) -> dict[str, Any]:
    severity_counts: dict[str, int] = {}
    items = []
    for alert in alerts:
        advisory = alert.get("security_advisory") or {}
        vulnerability = alert.get("security_vulnerability") or {}
        package = vulnerability.get("package") or {}
        severity = advisory.get("severity", "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        items.append({
            "number": alert.get("number"),
            "state": alert.get("state"),
            "severity": severity,
            "packageName": package.get("name"),
            "ecosystem": package.get("ecosystem"),
            "vulnerableVersionRange": vulnerability.get("vulnerable_version_range"),
            "firstPatchedVersion": (vulnerability.get("first_patched_version") or {}).get("identifier"),
            "summary": advisory.get("summary"),
            "cveId": advisory.get("cve_id"),
            "ghsaId": advisory.get("ghsa_id"),
            "url": alert.get("html_url"),
            "createdAt": alert.get("created_at"),
        })
    items.sort(key=lambda item: (item["severity"] != "critical", item["severity"] != "high", item["number"]))
    return {
        "repository": repository,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "openCount": len(items),
        "severityCounts": severity_counts,
        "alerts": items,
    }


def _existing_content(token: str, target_repository: str, path: str, branch: str) -> bytes | None:
    url = f"{API_URL}/repos/{target_repository}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}"
    try:
        response, _ = _api_request("GET", url, token)
    except CheckError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    if not isinstance(response, dict) or response.get("type") != "file":
        raise CheckError(f"Destination path {path} is not a file in {target_repository}.")
    return base64.b64decode(response["content"])


def publish(
    target_repository: str,
    files: dict[str, bytes],
    token: str,
    *,
    title: str = "Update Dependabot alert snapshots",
    branch_prefix: str = "dependabot-alerts",
    body: str = "Generated by scripts/check_dependabot_alerts.py -- open Dependabot alerts for every repository in the manifest.",
) -> dict[str, Any]:
    metadata, _ = _api_request("GET", f"{API_URL}/repos/{target_repository}", token)
    base_branch = metadata["default_branch"]
    planned = []
    for path, content in files.items():
        existing = _existing_content(token, target_repository, path, base_branch)
        action = "created" if existing is None else ("unchanged" if existing == content else "updated")
        planned.append((path, content, action))
    files_written = [{"path": path, "action": action} for path, _, action in planned]
    changed = [(path, content) for path, content, action in planned if action != "unchanged"]
    if not changed:
        return {"status": "unchanged", "pullRequestUrl": None, "filesWritten": files_written}

    ref, _ = _api_request("GET", f"{API_URL}/repos/{target_repository}/git/ref/heads/{urllib.parse.quote(base_branch, safe='')}", token)
    base_sha = ref["object"]["sha"]
    commit, _ = _api_request("GET", f"{API_URL}/repos/{target_repository}/git/commits/{base_sha}", token)
    base_tree = commit["tree"]["sha"]

    tree_items = []
    for path, content in changed:
        blob, _ = _api_request("POST", f"{API_URL}/repos/{target_repository}/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"), "encoding": "base64",
        })
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree, _ = _api_request("POST", f"{API_URL}/repos/{target_repository}/git/trees", token, {
        "base_tree": base_tree, "tree": tree_items,
    })
    new_commit, _ = _api_request("POST", f"{API_URL}/repos/{target_repository}/git/commits", token, {
        "message": title, "tree": tree["sha"], "parents": [base_sha],
    })
    branch_name = f"{branch_prefix}/{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    _api_request("POST", f"{API_URL}/repos/{target_repository}/git/refs", token, {
        "ref": f"refs/heads/{branch_name}", "sha": new_commit["sha"],
    })
    pull_request, _ = _api_request("POST", f"{API_URL}/repos/{target_repository}/pulls", token, {
        "title": title,
        "head": branch_name,
        "base": base_branch,
        "body": body,
    })
    return {"status": "created", "pullRequestUrl": pull_request["html_url"], "filesWritten": files_written}


def main() -> int:
    alerts_token = os.environ.get("DEPENDABOT_ALERTS_TOKEN", "")
    pr_token = os.environ.get("GITHUB_PR_TOKEN", "")
    if not alerts_token:
        print("DEPENDABOT_ALERTS_TOKEN is required.", file=sys.stderr)
        return 1
    if not pr_token:
        print("GITHUB_PR_TOKEN is required.", file=sys.stderr)
        return 1
    manifest_source_url = os.environ.get("MANIFEST_SOURCE_URL", DEFAULT_MANIFEST_SOURCE_URL)
    target_repository, _ = parse_manifest_source_url(manifest_source_url)

    manifest = fetch_manifest(manifest_source_url)
    repositories = manifest_repositories(manifest)
    print(f"Loaded manifest with {len(repositories)} repositories.", file=sys.stderr)

    files: dict[str, bytes] = {}
    failures = []
    for repository in repositories:
        print(f"--- {repository} ---", file=sys.stderr)
        try:
            alerts = fetch_open_alerts(repository, alerts_token)
            snapshot = summarize_alerts(repository, alerts)
            content = (json.dumps(snapshot, indent=2) + "\n").encode("utf-8")
            files[f"{repository.split('/')[-1]}/dependency-alerts/dependabot-alerts.json"] = content
            print(f"  {snapshot['openCount']} open alert(s): {snapshot['severityCounts']}", file=sys.stderr)
        except CheckError as error:
            failures.append({"repository": repository, "message": str(error)})
            print(f"  FAILED: {error}", file=sys.stderr)

    if not files:
        print("No alert snapshots generated -- not publishing a PR.", file=sys.stderr)
        print(json.dumps({"failures": failures}, indent=2))
        return 1

    result = publish(target_repository, files, pr_token)
    result["failures"] = failures
    print(json.dumps(result, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
