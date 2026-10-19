"""Responses host for deterministic user-journey (navigation graph) discovery."""

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
            or set(payload) != {"sourceUrl"}
            or not isinstance(payload["sourceUrl"], str)
            or not payload["sourceUrl"].strip()
        ):
            raise ScanError("invalid_input", 'Input must contain exactly one non-empty "sourceUrl" property.')
        result = await asyncio.to_thread(scan, payload["sourceUrl"].strip())
    except Exception as error:
        # Report failures rather than an empty graph, so a download error is never published as "no pages".
        logger.exception("User-journeys source discovery failed.")
        code = error.code if isinstance(error, ScanError) else "discovery_failed"
        message = str(error) if isinstance(error, ScanError) else "User-journeys source discovery failed."
        result = {"error": {"code": code, "message": message}}
    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
