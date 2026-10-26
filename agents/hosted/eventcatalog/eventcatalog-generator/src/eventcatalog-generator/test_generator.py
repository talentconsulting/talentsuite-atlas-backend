import json
import unittest
from unittest import mock

import generator
from generator import GenerationError, SourceLocation, generate_from_text, parse_input, source_prompt, validate_catalog


SOURCE = "https://github.com/source/app/tree/main/src/Application"
FILES = ["https://github.com/source/app/blob/main/src/Application/Commands/CreateOrderCommand.cs"]
CATALOG = {
    "repository": "source/app",
    "ref": "main",
    "path": "src/Application",
    "commands": [
        {
            "name": "CreateOrderCommand",
            "namespace": "App.Commands",
            "sourceFile": "src/Application/Commands/CreateOrderCommand.cs",
            "description": None,
            "fields": [{"name": "orderId", "type": "Guid", "required": True, "description": None}],
            "handlers": [{"name": "CreateOrderHandler", "sourceFile": "src/Application/Handlers/CreateOrderHandler.cs"}], "senders": [],
        }
    ],
    "events": [],
}


class GeneratorTests(unittest.TestCase):
    def test_source_prompt_explicitly_requests_json_for_json_object_mode(self):
        prompt = source_prompt(
            SourceLocation("source", "app", "main", "src/Application"),
            {"src/Application/Commands/CreateOrderCommand.cs": "record CreateOrderCommand();"},
        )

        self.assertIn("JSON", prompt)

    def test_input_requires_selected_same_repository_files(self):
        payload = parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES}))
        self.assertEqual(FILES, payload["sourceFiles"])
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE}))
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": ["https://github.com/other/app/blob/main/X.cs"]}))

    def test_validates_catalog_contract(self):
        location = SourceLocation("source", "app", "main", "src/Application")
        self.assertEqual(CATALOG, validate_catalog(json.loads(json.dumps(CATALOG)), location))

    def test_rejects_invented_source_identity_and_bad_fields(self):
        wrong = json.loads(json.dumps(CATALOG))
        wrong["repository"] = "other/app"
        with self.assertRaisesRegex(GenerationError, "identity"):
            validate_catalog(wrong, SourceLocation("source", "app", "main", "src/Application"))
        malformed = json.loads(json.dumps(CATALOG))
        malformed["commands"][0]["fields"][0]["required"] = "yes"
        with self.assertRaisesRegex(GenerationError, "boolean"):
            validate_catalog(malformed)

    def test_generate_downloads_only_selected_files_and_validates_output(self):
        loaded = []

        def load(location, files, tenant=None):
            loaded.extend(files)
            return {"src/Application/Commands/CreateOrderCommand.cs": "record CreateOrderCommand(Guid orderId);"}

        result = generate_from_text(
            json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES}),
            completion=lambda prompt: json.dumps(CATALOG),
            source_loader=load,
        )

        self.assertEqual(FILES, loaded)
        self.assertEqual("CreateOrderCommand", result["commands"][0]["name"])

    def test_sorts_messages_deterministically(self):
        catalog = json.loads(json.dumps(CATALOG))
        second = json.loads(json.dumps(catalog["commands"][0]))
        second["name"] = "ApproveOrderCommand"
        catalog["commands"].append(second)
        result = validate_catalog(catalog)
        self.assertEqual(["ApproveOrderCommand", "CreateOrderCommand"], [item["name"] for item in result["commands"]])


class SenderTests(unittest.TestCase):
    SOURCES = {
        "src/Commitments/Extensions/RoutingSettingsExtensions.cs": """using SFA.DAS.Notifications.Messages.Commands;
using SFA.DAS.CommitmentsV2.Messages.Commands;
static class Routing { static void Add(RoutingSettings routing) {
    routing.RouteToEndpoint(typeof(SendEmailCommand), "SFA.DAS.Notifications.MessageHandlers");
} }""",
        "src/Commitments/Handlers/SendEmailToEmployerCommandHandler.cs": """using SFA.DAS.Notifications.Messages.Commands;
using SFA.DAS.CommitmentsV2.Messages.Commands;
class SendEmailToEmployerCommandHandler : IHandleMessages<SendEmailToEmployerCommand> {
    async Task Handle(SendEmailToEmployerCommand message) {
        var email = BuildEmail(message);
        await _messageSession.Send(new SendEmailCommand(email.TemplateId, email.To, email.Tokens));
        await _mediator.Send(new UpdateApprovalCommand(message.Id));
    }
}""",
        "src/Commitments/Services/Approvals.cs": """using SFA.DAS.CommitmentsV2.Messages.Events;
class Approvals { async Task Approve() {
    ApprenticeshipApprovedEvent approved = new() { Id = 1 };
    await _eventPublisher.Publish(approved);
} }""",
        "src/Commitments/Plain.cs": "class Plain { void A() { _session.Send(new SendEmailCommand()); } }",
    }

    def test_finds_routed_sent_and_published_messages_with_their_namespace(self):
        from generator import sent_messages
        found = sent_messages(self.SOURCES, "SkillsFundingAgency/das-commitments")
        self.assertIn(("SendEmailCommand", "SFA.DAS.Notifications.Messages.Commands", "src/Commitments/Extensions/RoutingSettingsExtensions.cs"), found)
        self.assertIn(("SendEmailCommand", "SFA.DAS.Notifications.Messages.Commands", "src/Commitments/Handlers/SendEmailToEmployerCommandHandler.cs"), found)
        self.assertIn(("ApprenticeshipApprovedEvent", "SFA.DAS.CommitmentsV2.Messages.Events", "src/Commitments/Services/Approvals.cs"), found)
        names = [name for name, _, _ in found]
        self.assertNotIn("UpdateApprovalCommand", names)          # MediatR
        self.assertNotIn("SendEmailToEmployerCommand", names)     # handled here, not sent
        self.assertEqual(3, len(found))                           # Plain.cs imports no messages namespace

    def test_adds_other_services_messages_with_senders_and_no_handlers(self):
        from generator import add_senders
        document = {"repository": "SkillsFundingAgency/das-commitments", "ref": "main", "path": "src", "commands": [], "events": []}
        result = add_senders(document, self.SOURCES)
        [email] = [m for m in result["commands"] if m["name"] == "SendEmailCommand"]
        self.assertEqual("SFA.DAS.Notifications.Messages.Commands", email["namespace"])
        self.assertEqual([], email["handlers"])
        self.assertEqual(["RoutingSettingsExtensions", "SendEmailToEmployerCommandHandler"], [s["name"] for s in email["senders"]])

    def test_a_message_without_a_source_file_takes_its_senders_or_is_dropped(self):
        from generator import validate_catalog
        message = lambda name, senders: {"name": name, "namespace": "N", "sourceFile": "", "description": None,
                                         "fields": [], "handlers": [], "senders": senders}
        document = {"repository": "o/r", "ref": "main", "path": "src", "events": [],
                    "commands": [message("Sent", [{"name": "S", "sourceFile": "src/S.cs"}]), message("NoEvidence", [])]}
        result = validate_catalog(document)
        self.assertEqual([("Sent", "src/S.cs")], [(m["name"], m["sourceFile"]) for m in result["commands"]])

    def test_senders_join_a_message_the_model_already_listed(self):
        from generator import add_senders
        document = {"repository": "SkillsFundingAgency/das-commitments", "ref": "main", "path": "src", "commands": [], "events": [
            {"name": "ApprenticeshipApprovedEvent", "namespace": "SFA.DAS.CommitmentsV2.Messages.Events", "sourceFile": "src/Events/A.cs",
             "description": None, "fields": [], "handlers": [], "senders": []}]}
        result = add_senders(document, self.SOURCES)
        self.assertEqual(1, len(result["events"]))
        self.assertEqual(["src/Commitments/Services/Approvals.cs"], [s["sourceFile"] for s in result["events"][0]["senders"]])


class TenantTests(unittest.TestCase):
    def test_passes_the_tenant_to_the_source_loader(self):
        seen = []

        def load(location, files, tenant=None):
            seen.append(tenant)
            return {"src/Application/Commands/CreateOrderCommand.cs": "record CreateOrderCommand(Guid orderId);"}

        generate_from_text(
            json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES, "tenant": "tenant/atlas"}),
            completion=lambda prompt: json.dumps(CATALOG),
            source_loader=load,
        )
        self.assertEqual(["tenant/atlas"], seen)

    def test_tenant_is_optional(self):
        self.assertIsNone(parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES}))["tenant"])

    def test_rejects_a_tenant_that_is_not_a_non_empty_string(self):
        for tenant in (1, "", "  "):
            with self.subTest(tenant=tenant), self.assertRaisesRegex(GenerationError, "tenant"):
                parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES, "tenant": tenant}))


class SourceDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"record CreateOrderCommand();"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(generator.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(generator.urllib.request, "urlopen", side_effect=urlopen):
            generator._download_selected_sources(generator.parse_source_url(SOURCE), FILES, "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_reads_through_the_contents_api(self):
        request = self.download("installation-token")
        self.assertEqual(
            "https://api.github.com/repos/source/app/contents/src/Application/Commands/CreateOrderCommand.cs?ref=main",
            request.full_url,
        )
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))
        self.assertEqual("application/vnd.github.raw", request.get_header("Accept"))

    def test_without_a_token_the_raw_file_is_read(self):
        request = self.download(None)
        self.assertEqual(
            "https://raw.githubusercontent.com/source/app/main/src/Application/Commands/CreateOrderCommand.cs",
            request.full_url,
        )
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
