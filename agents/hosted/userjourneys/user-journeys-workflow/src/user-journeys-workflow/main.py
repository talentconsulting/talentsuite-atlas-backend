"""Responses-protocol host for the user-journeys workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponseEventStream, ResponsesAgentServerHost
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from workflow import WorkflowError, parse_workflow_request, run_workflow


logger = logging.getLogger(__name__)
PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
MODEL = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4o")
DISCOVERY_NAME = os.getenv("USER_JOURNEYS_DISCOVERY_AGENT_NAME", "user-journeys-source-discovery")
GENERATOR_NAME = os.getenv("USER_JOURNEYS_GENERATOR_AGENT_NAME", "user-journeys-generator")
PR_CREATOR_NAME = os.getenv("USER_JOURNEYS_PR_CREATOR_AGENT_NAME", "user-journeys-pr-creator")
GENERATOR_CONCURRENCY = int(os.getenv("USER_JOURNEYS_GENERATOR_CONCURRENCY", "3"))
if not PROJECT_ENDPOINT:
    raise EnvironmentError("FOUNDRY_PROJECT_ENDPOINT is required.")

project_client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())
app = ResponsesAgentServerHost()


def error_response(error: Exception, source_url: str = "") -> dict[str, Any]:
    code = error.code if isinstance(error, WorkflowError) else "workflow_failed"
    message = str(error) if isinstance(error, WorkflowError) else "The user-journeys workflow could not be completed."
    return {
        "success": False,
        "sourceUrl": source_url,
        "generatedCatalogCount": 0,
        "pageCount": 0,
        "journeyCount": 0,
        "generationErrors": [],
        "catalogs": [],
        "pullRequest": None,
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
    source_url = ""
    try:
        payload = parse_workflow_request(await context.get_input_text() or "")
        source_url = payload["sourceUrl"]
        result = await asyncio.to_thread(
            run_workflow,
            project_client,
            payload,
            DISCOVERY_NAME,
            GENERATOR_NAME,
            PR_CREATOR_NAME,
            MODEL,
            GENERATOR_CONCURRENCY,
        )
    except Exception as error:
        logger.exception("User-journeys workflow failed.")
        result = error_response(error, source_url)
    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
