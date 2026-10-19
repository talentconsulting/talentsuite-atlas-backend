"""Responses host for manifest-driven user-journeys generation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponseEventStream, ResponsesAgentServerHost
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from orchestrator import ManifestError, parse_request, run_manifest


logger = logging.getLogger(__name__)
PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
MODEL = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4o")
WORKFLOW_NAME = os.getenv("USER_JOURNEYS_WORKFLOW_AGENT_NAME", "user-journeys-workflow")
PUBLISHER_NAME = os.getenv("USER_JOURNEYS_PR_CREATOR_AGENT_NAME", "user-journeys-pr-creator")
MAX_ENTRIES = int(os.getenv("USER_JOURNEYS_MANIFEST_MAX_ENTRIES", "100"))
MAX_CATALOGS = int(os.getenv("USER_JOURNEYS_MANIFEST_MAX_CATALOGS", "100"))
WORKFLOW_CONCURRENCY = int(os.getenv("USER_JOURNEYS_MANIFEST_WORKFLOW_CONCURRENCY", "4"))
if not PROJECT_ENDPOINT:
    raise EnvironmentError("FOUNDRY_PROJECT_ENDPOINT is required.")

project_client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())
app = ResponsesAgentServerHost()


def error_response(error: Exception, source_url: str = "") -> dict[str, Any]:
    code = error.code if isinstance(error, ManifestError) else "orchestration_failed"
    message = str(error) if isinstance(error, ManifestError) else (str(error).strip() or "The manifest workflow could not be completed.")[:300]
    return {
        "success": False,
        "status": "failed",
        "sourceUrl": source_url,
        "checkedCount": 0,
        "changedCount": 0,
        "generatedRepositoryCount": 0,
        "generatedCatalogCount": 0,
        "upToDate": [],
        "failures": [{"repository": "", "stage": "orchestration", "errorType": code, "message": message}],
        "pullRequest": None,
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
        payload = parse_request(await context.get_input_text() or "")
        source_url = payload["sourceUrl"]
        result = await asyncio.to_thread(
            run_manifest,
            project_client,
            payload,
            WORKFLOW_NAME,
            PUBLISHER_NAME,
            MODEL,
            MAX_ENTRIES,
            MAX_CATALOGS,
            WORKFLOW_CONCURRENCY,
        )
    except Exception as error:
        logger.exception("Manifest-driven user-journeys orchestration failed.")
        result = error_response(error, source_url)
    yield text.emit_delta(json.dumps(result, separators=(",", ":")))
    yield text.emit_text_done()
    yield text.emit_done()
    yield message.emit_done()
    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
