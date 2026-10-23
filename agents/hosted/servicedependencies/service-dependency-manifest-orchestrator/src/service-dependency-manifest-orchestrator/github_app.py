"""GitHub credentials for one tenant, minted from the Atlas GitHub App.

A tenant is identified by its data repository ("owner/repo"): the repository holding its manifest and
generated catalogues. With GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY set, every token is a short-lived
installation token for the App installation that covers that repository, so a run for one tenant cannot
reach another tenant's repositories. Tokens are minted inside the agent and never travel in a request.

Without the App settings (or without a tenant) the agent falls back to its configured personal access
token, GITHUB_READ_TOKEN or GITHUB_PR_TOKEN, as before.

Keep this file identical in every agent that uses it.
"""
from __future__ import annotations

import calendar
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

API_URL = "https://api.github.com"
READ_PERMISSIONS = {"contents": "read", "metadata": "read"}
WRITE_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
# Mint a fresh token this long before the cached one expires (installation tokens last an hour).
REFRESH_MARGIN_SECONDS = 300

_cache: dict[tuple[Any, ...], tuple[str, float]] = {}
_lock = threading.Lock()


class GitHubAppError(Exception):
    pass


def app_configured() -> bool:
    return bool(os.getenv("GITHUB_APP_ID") and os.getenv("GITHUB_APP_PRIVATE_KEY"))


def parse_repository(value: str) -> tuple[str, str]:
    parts = str(value or "").strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubAppError(f"Tenant must be an owner/repo data repository, got {value!r}.")
    return parts[0], parts[1].removesuffix(".git")


def _app_jwt() -> str:
    import jwt  # PyJWT[crypto]; imported here so agents without the App configured do not need it.

    now = int(time.time())
    # Connections often store the PEM with literal "\n" sequences.
    key = os.environ["GITHUB_APP_PRIVATE_KEY"].replace("\\n", "\n")
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": os.environ["GITHUB_APP_ID"]}, key, algorithm="RS256")


def _call(method: str, path: str, bearer: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "talentsuite-atlas",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404 and path.endswith("/installation"):
            raise GitHubAppError(f"The Atlas GitHub App is not installed on {path.split('/')[2]}/{path.split('/')[3]}.") from error
        raise GitHubAppError(f"GitHub returned HTTP {error.code} for {method} {path}.") from error
    except urllib.error.URLError as error:
        raise GitHubAppError("GitHub could not be reached.") from error


def _mint(tenant: str, repositories: tuple[str, ...], permissions: dict[str, str]) -> str:
    key = (tenant, repositories, tuple(sorted(permissions.items())))
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
        owner, repository = parse_repository(tenant)
        app_token = _app_jwt()
        installation = _call("GET", f"/repos/{owner}/{repository}/installation", app_token)["id"]
        body: dict[str, Any] = {"permissions": permissions}
        if repositories:
            body["repositories"] = list(repositories)
        result = _call("POST", f"/app/installations/{installation}/access_tokens", app_token, body)
        expires = calendar.timegm(time.strptime(result["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        _cache[key] = (result["token"], expires)
        return result["token"]


def read_token(tenant: str | None) -> str | None:
    """Token for reading source repositories on the tenant's behalf: the tenant's installation, read-only."""
    if tenant and app_configured():
        return _mint(tenant, (), READ_PERMISSIONS)
    return os.getenv("GITHUB_READ_TOKEN") or None


def write_token(repository: str) -> str:
    """Token for opening a pull request on a tenant's data repository, limited to that one repository."""
    if app_configured():
        _owner, name = parse_repository(repository)
        return _mint(repository, (name,), WRITE_PERMISSIONS)
    return os.getenv("GITHUB_PR_TOKEN", "")
