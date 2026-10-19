"""Responses-protocol host for the user-journeys generator."""

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

from generator import GenerationError, generate_from_text


logger = logging.getLogger(__name__)
app = ResponsesAgentServerHost()


def error_response(error: Exception) -> dict[str, object]:
    if isinstance(error, GenerationError):
        code = error.code
        message = str(error)
    else:
        code = "generation_failed"
        message = "The user journeys could not be generated."
    return {"error": {"code": code, "message": message}}


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
        result = await asyncio.to_thread(
            generate_from_text,
            await context.get_input_text() or "",
        )
    except Exception as error:
        logger.exception("User-journeys generation failed.")
        result = error_response(error)

    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
