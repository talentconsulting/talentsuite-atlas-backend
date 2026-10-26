"""Per-tenant model usage, recorded in the environment's usage ledger for billing.

Each model call adds one row to the ledger table: the month, the tenant (its owner/repo data repository),
the agent, the model deployment and the token counts. USAGE_LEDGER_URL is a SAS URL for the table that can
only add rows; provisioning stores it in the atlas-usage-ledger project connection. Without it nothing is
recorded. A row that can't be written is logged and dropped, so metering never fails a generation.

Keep this file identical in every agent that uses it.
"""
from __future__ import annotations

import contextvars
import datetime
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

UNASSIGNED_TENANT = "unassigned"

_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("usage_ledger_tenant", default=None)
logger = logging.getLogger(__name__)


def begin(user_input: str) -> None:
    """Remember the request's tenant for the model calls made while handling it."""
    try:
        tenant = json.loads(user_input).get("tenant")
    except (ValueError, AttributeError):
        tenant = None
    _tenant.set(tenant.strip() if isinstance(tenant, str) and tenant.strip() else None)


def _count(value: Any, name: str) -> int:
    number = getattr(value, name, None)
    return number if isinstance(number, int) and number > 0 else 0


def _key(value: str) -> str:
    # Table keys can't contain / \ # ? or control characters.
    return re.sub(r"[/\\#?\x00-\x1f\x7f]", ":", value)


def usage_entity(response: Any, agent: str, model: str, tenant: str | None, now: datetime.datetime) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    tenant = tenant or UNASSIGNED_TENANT
    return {
        "PartitionKey": now.strftime("%Y-%m"),
        "RowKey": f"{_key(tenant)}|{now.strftime('%Y%m%dT%H%M%S%fZ')}|{uuid.uuid4().hex}",
        "Tenant": tenant,
        "Agent": agent,
        "Model": model,
        "InputTokens": _count(usage, "input_tokens"),
        "CachedInputTokens": _count(getattr(usage, "input_tokens_details", None), "cached_tokens"),
        "OutputTokens": _count(usage, "output_tokens"),
    }


def record(response: Any, agent: str, model: str) -> None:
    """Add one ledger row for a completed model response. Never raises."""
    url = os.getenv("USAGE_LEDGER_URL", "").strip()
    if not url:
        return
    try:
        entity = usage_entity(response, agent, model, _tenant.get(), datetime.datetime.now(datetime.timezone.utc))
        request = urllib.request.Request(
            url,
            method="POST",
            data=json.dumps(entity).encode(),
            headers={
                "Accept": "application/json;odata=nometadata",
                "Content-Type": "application/json",
                "Prefer": "return-no-content",
                "x-ms-version": "2019-02-02",
            },
        )
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.HTTPError as error:
        # The URL carries the SAS token, so it is never logged.
        logger.warning("Usage not recorded: the ledger returned HTTP %s.", error.code)
    except Exception as error:  # noqa: BLE001 - metering must not fail the request.
        logger.warning("Usage not recorded: %s.", type(error).__name__)
