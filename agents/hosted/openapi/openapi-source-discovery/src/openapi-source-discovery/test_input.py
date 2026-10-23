import json
import sys
import types
import unittest


# Keep the input-contract unit test independent of the hosted runtime dependency.
responses = types.ModuleType("azure.ai.agentserver.responses")
responses.CreateResponse = object
responses.ResponseContext = object
responses.ResponseEventStream = object
responses.ResponsesAgentServerHost = lambda: types.SimpleNamespace(
    response_handler=lambda function: function,
    run=lambda: None,
)
sys.modules.setdefault("azure", types.ModuleType("azure"))
sys.modules.setdefault("azure.ai", types.ModuleType("azure.ai"))
sys.modules.setdefault("azure.ai.agentserver", types.ModuleType("azure.ai.agentserver"))
sys.modules.setdefault("azure.ai.agentserver.responses", responses)

from main import extract_request
from scanner import ScanError


class InputTests(unittest.TestCase):
    def test_accepts_exactly_one_source_url_property(self):
        value = "https://github.com/example/repo/tree/main/src"
        self.assertEqual(extract_request(json.dumps({"sourceUrl": value})), (value, None))

    def test_accepts_an_optional_tenant(self):
        value = "https://github.com/example/repo/tree/main/src"
        self.assertEqual(
            extract_request(json.dumps({"sourceUrl": value, "tenant": "tenant/atlas"})),
            (value, "tenant/atlas"),
        )

    def test_rejects_a_non_string_tenant(self):
        with self.assertRaises(ScanError):
            extract_request(json.dumps({"sourceUrl": "https://example.test", "tenant": 1}))

    def test_rejects_additional_properties(self):
        with self.assertRaises(ScanError):
            extract_request(
                json.dumps({"sourceUrl": "https://example.test", "path": "src"})
            )


if __name__ == "__main__":
    unittest.main()
