"""Responses-protocol host for the OpenAPI Source Discovery scanner."""

from __future__ import annotations

import asyncio
import json
import logging

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
)

from scanner import ScanError, scan


logger = logging.getLogger(__name__)
app = ResponsesAgentServerHost()


def extract_request(user_input: str) -> tuple[str, str | None]:
    try:
        payload = json.loads(user_input.strip())
    except json.JSONDecodeError as error:
        raise ScanError("Input must be valid JSON.") from error
    if (
        not isinstance(payload, dict)
        or "sourceUrl" not in payload
        or not set(payload) <= {"sourceUrl", "tenant"}
        or not isinstance(payload["sourceUrl"], str)
        or not payload["sourceUrl"].strip()
    ):
        raise ScanError('Input must contain a non-empty "sourceUrl" and an optional "tenant" (the owner/repo data repository).')
    tenant = payload.get("tenant")
    if tenant is not None and not isinstance(tenant, str):
        raise ScanError('"tenant" must be an owner/repo string.')
    return payload["sourceUrl"].strip(), tenant


@app.response_handler
async def handle_create(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    stream = ResponseEventStream(response_id=context.response_id, request=request)
    yield stream.emit_created()
    yield stream.emit_in_progress()
    message = stream.add_output_item_message()
    yield message.emit_added()
    text = message.add_text_content()
    yield text.emit_added()

    try:
        source_url, tenant = extract_request(await context.get_input_text() or "")
        result = await asyncio.to_thread(scan, source_url, tenant=tenant)
    except Exception as error:
        # A scan failure (bad ref, network error, malformed archive) is not the same as
        # legitimately finding zero API files -- reporting it as an empty list here would let the
        # workflow treat a failed scan as a successful empty one and advance the manifest's commit
        # hash as though this repository had actually been checked.
        logger.exception("OpenAPI Source Discovery scan failed.")
        result = {"error": {"code": "scan_failed", "message": str(error)[:300]}}

    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
