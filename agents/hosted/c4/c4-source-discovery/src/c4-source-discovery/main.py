"""Responses host for deterministic c4 source discovery."""

from __future__ import annotations

import asyncio
import json
import logging

from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponseEventStream, ResponsesAgentServerHost

from scanner import ScanError, scan


logger = logging.getLogger(__name__)
app = ResponsesAgentServerHost()


@app.response_handler
async def handle_create(request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event):
    stream = ResponseEventStream(response_id=context.response_id, request=request)
    yield stream.emit_created()
    yield stream.emit_in_progress()
    message = stream.add_output_item_message()
    yield message.emit_added()
    text = message.add_text_content()
    yield text.emit_added()
    try:
        payload = json.loads(await context.get_input_text() or "")
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
        result = await asyncio.to_thread(scan, payload["sourceUrl"].strip(), tenant=tenant)
    except Exception:
        logger.exception("C4 source discovery failed.")
        result = {"sourceFiles": [], "excludedFiles": []}
    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
