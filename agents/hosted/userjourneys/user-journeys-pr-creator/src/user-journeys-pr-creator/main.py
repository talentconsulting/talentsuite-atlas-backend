"""Responses-protocol host for the user-journeys PR creator."""

from __future__ import annotations

import asyncio
import json
import logging

from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponseEventStream, ResponsesAgentServerHost

from github_pr import PublicationError, parse_request, publish


logger = logging.getLogger(__name__)
app = ResponsesAgentServerHost()


def error_response(error: Exception) -> dict[str, object]:
    code = error.code if isinstance(error, PublicationError) else "publication_failed"
    message = str(error) if isinstance(error, PublicationError) else "The user journeys could not be published."
    return {
        "success": False, "status": "failed", "repository": "", "branchName": "", "commitSha": "",
        "pullRequestUrl": "", "pullRequestNumber": 0, "filesWritten": [],
        "errors": [{"code": code, "message": message}],
    }


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
        result = await asyncio.to_thread(publish, parse_request(await context.get_input_text() or ""))
    except Exception as error:
        logger.exception("User-journeys publication failed.")
        result = error_response(error)
    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
