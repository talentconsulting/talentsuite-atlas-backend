import threading
import unittest

from workflow import WorkflowError, merge_catalogs, parse_workflow_request, run_workflow, validate_discovery_output


SOURCE = "https://github.com/source/app/tree/main/src/Application"
FILES = [f"https://github.com/source/app/blob/main/src/Application/Commands/Command{index}.cs" for index in range(3)]


def catalog(commands=None, events=None):
    return {
        "repository": "source/app", "ref": "main", "path": "src/Application",
        "commands": commands or [], "events": events or [],
    }


def message(name):
    return {"name": name, "namespace": "App", "sourceFile": f"src/{name}.cs", "description": None, "fields": [], "handlers": []}


class WorkflowTests(unittest.TestCase):
    def test_direct_request_requires_target_repository(self):
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request('{"sourceUrl":"%s"}' % SOURCE)
        deferred = parse_workflow_request('{"sourceUrl":"%s","deferPublication":true}' % SOURCE)
        self.assertTrue(deferred["deferPublication"])

    def test_discovery_accepts_only_same_repository_blob_urls(self):
        result = validate_discovery_output({"sourceFiles": FILES, "excludedFiles": []}, SOURCE, 10)
        self.assertEqual(FILES, result["sourceFiles"])
        with self.assertRaises(WorkflowError):
            validate_discovery_output({"sourceFiles": ["https://github.com/other/app/blob/main/X.cs"], "excludedFiles": []}, SOURCE, 10)

    def test_discovery_accepts_an_empty_source_files_list(self):
        result = validate_discovery_output({"sourceFiles": [], "excludedFiles": []}, SOURCE, 10)
        self.assertEqual([], result["sourceFiles"])

    def test_deferred_workflow_batches_and_merges_catalogs(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            if name == "generator":
                index = FILES.index(payload["sourceFiles"][0])
                return catalog(commands=[message(f"Command{index}")])
            raise AssertionError(name)

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o",
            generator_batch_size=1, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedCatalogCount"])
        self.assertEqual(3, len(result["catalogs"][0]["catalog"]["commands"]))
        self.assertEqual(3, len([call for call in calls if call[0] == "generator"]))

    def test_direct_workflow_publishes_deterministic_path(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            if name == "generator":
                return catalog(events=[message("OrderCreatedEvent")])
            self.assertEqual("publisher", name)
            self.assertEqual("app/event-catalog/events-and-commands.json", payload["catalogs"][0]["targetPath"])
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/catalog/pull/1"}

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "targetRepository": "target/catalog"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual("created", result["pullRequest"]["status"])

    def test_failed_batch_fails_closed_and_returns_no_partial_catalog(self):
        publisher_called = False

        def invoke(project, name, model, payload, max_attempts=2):
            nonlocal publisher_called
            if name == "discovery":
                return {"sourceFiles": FILES[:2], "excludedFiles": []}
            if name == "publisher":
                publisher_called = True
                raise AssertionError("A partial catalog must not be published.")
            if payload["sourceFiles"] == FILES[1:2]:
                return {"error": {"code": "invalid_model_output", "message": "bad catalog"}}
            return catalog(commands=[message("CreateOrderCommand")])

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o",
            generator_batch_size=1, invoker=invoke, retry_delay_seconds=0,
        )
        self.assertFalse(result["success"])
        self.assertEqual("partial_generation_failed", result["errors"][0]["code"])
        self.assertEqual(1, len(result["generationErrors"]))
        self.assertEqual([], result["catalogs"])
        self.assertFalse(publisher_called)

    def test_empty_discovery_produces_an_empty_but_successful_catalog(self):
        # No candidate files at all is a legitimate outcome (e.g. a repository with no pub/sub
        # activity) -- the generator must not even be invoked in this case.
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": [], "excludedFiles": []}
            raise AssertionError("The generator must not be called when discovery finds nothing.")

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["commands"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["events"])

    def test_no_messages_identified_is_a_valid_successful_result(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            return catalog()

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["commands"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["events"])

    def test_merge_combines_handlers_for_the_same_message(self):
        declaration = message("CreateOrder")
        handler_view = message("CreateOrder")
        handler_view["sourceFile"] = "src/CreateOrderHandler.cs"
        handler_view["handlers"] = [{"name": "CreateOrderHandler", "sourceFile": "src/CreateOrderHandler.cs"}]
        merged, warnings = merge_catalogs([catalog(commands=[declaration]), catalog(commands=[handler_view])])
        self.assertEqual(["CreateOrderHandler"], [item["name"] for item in merged["commands"][0]["handlers"]])
        self.assertEqual([], warnings)

    def test_merge_prefers_declaration_fields_over_handler_inference(self):
        handler_view = message("CreateOrder")
        handler_view["sourceFile"] = "src/CreateOrderHandler.cs"
        handler_view["fields"] = [{"name": "AccountId", "type": "int", "required": None, "description": None}]
        handler_view["handlers"] = [{"name": "CreateOrderHandler", "sourceFile": "src/CreateOrderHandler.cs"}]
        declaration = message("CreateOrder")
        declaration["fields"] = [{"name": "AccountId", "type": "long", "required": True, "description": None}]

        merged, warnings = merge_catalogs([catalog(commands=[handler_view]), catalog(commands=[declaration])])

        command = merged["commands"][0]
        self.assertEqual("src/CreateOrder.cs", command["sourceFile"])
        self.assertEqual(declaration["fields"], command["fields"])
        self.assertEqual(handler_view["handlers"], command["handlers"])
        self.assertEqual([], warnings)

    def test_merge_recognizes_declaration_filename_with_trailing_space(self):
        handler_view = message("OrderCreatedEvent")
        handler_view["sourceFile"] = "src/OrderCreatedEventHandler.cs"
        declaration = message("OrderCreatedEvent")
        declaration["sourceFile"] = "src/OrderCreatedEvent .cs"

        merged, warnings = merge_catalogs([catalog(events=[handler_view]), catalog(events=[declaration])])

        self.assertEqual("src/OrderCreatedEvent .cs", merged["events"][0]["sourceFile"])
        self.assertEqual([], warnings)

    def test_merge_recognizes_declaration_filename_without_event_suffix(self):
        handler_view = message("OrderCreatedEvent")
        handler_view["sourceFile"] = "src/OrderCreatedEventHandler.cs"
        handler_view["fields"] = [{"name": "OrderId", "type": "int", "required": None, "description": None}]
        declaration = message("OrderCreatedEvent")
        declaration["sourceFile"] = "src/OrderCreated.cs"
        declaration["fields"] = [{"name": "OrderId", "type": "long", "required": True, "description": None}]

        merged, warnings = merge_catalogs([catalog(events=[handler_view]), catalog(events=[declaration])])

        self.assertEqual("src/OrderCreated.cs", merged["events"][0]["sourceFile"])
        self.assertEqual(declaration["fields"], merged["events"][0]["fields"])
        self.assertEqual([], warnings)

    def test_merge_keeps_first_field_and_warns_on_a_genuine_conflict(self):
        first = message("ImportAccountPaymentMetadataCommand")
        first["fields"] = [{"name": "PeriodEndRef", "type": "string", "required": True, "description": None}]
        second = message("ImportAccountPaymentMetadataCommand")
        second["sourceFile"] = first["sourceFile"]
        second["fields"] = [{"name": "PeriodEndRef", "type": "int", "required": False, "description": None}]

        merged, warnings = merge_catalogs([catalog(commands=[first]), catalog(commands=[second])])

        self.assertEqual(first["fields"], merged["commands"][0]["fields"])
        self.assertEqual(1, len(warnings))
        self.assertEqual("ConflictingField", warnings[0]["errorType"])
        self.assertIn("PeriodEndRef", warnings[0]["message"])

    def test_merge_keeps_first_declaration_and_warns_on_conflicting_source_files(self):
        first = message("FoundLevyPayerEmployerAccount")
        first["sourceFile"] = "src/Events/FoundLevyPayerEmployerAccount.cs"
        second = message("FoundLevyPayerEmployerAccount")
        second["sourceFile"] = "src/FoundLevyPayerEmployerAccount.cs"

        merged, warnings = merge_catalogs([catalog(events=[first]), catalog(events=[second])])

        self.assertEqual(first["sourceFile"], merged["events"][0]["sourceFile"])
        self.assertEqual(1, len(warnings))
        self.assertEqual("ConflictingSourceFile", warnings[0]["errorType"])
        self.assertIn("FoundLevyPayerEmployerAccount", warnings[0]["message"])

    def test_deferred_workflow_surfaces_merge_warnings_but_still_succeeds(self):
        first = message("ImportAccountPaymentMetadataCommand")
        first["fields"] = [{"name": "PeriodEndRef", "type": "string", "required": True, "description": None}]
        second = message("ImportAccountPaymentMetadataCommand")
        second["sourceFile"] = first["sourceFile"]
        second["fields"] = [{"name": "PeriodEndRef", "type": "int", "required": False, "description": None}]

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:2], "excludedFiles": []}
            index = FILES.index(payload["sourceFiles"][0])
            return catalog(commands=[first if index == 0 else second])

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o",
            generator_batch_size=1, invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, len(result["catalogs"][0]["catalog"]["commands"]))
        self.assertEqual(1, len(result["generationErrors"]))
        self.assertEqual("ConflictingField", result["generationErrors"][0]["errorType"])

    def test_batches_are_generated_concurrently(self):
        # Fails with BrokenBarrierError unless all three batches are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            barrier.wait()
            index = FILES.index(payload["sourceFiles"][0])
            return catalog(commands=[message(f"Command{index}")])

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o",
            generator_batch_size=1, generator_concurrency=3, invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(3, len(result["catalogs"][0]["catalog"]["commands"]))

    def test_merge_follows_batch_order_not_completion_order(self):
        first = message("ImportAccountPaymentMetadataCommand")
        first["fields"] = [{"name": "PeriodEndRef", "type": "string", "required": True, "description": None}]
        second = message("ImportAccountPaymentMetadataCommand")
        second["fields"] = [{"name": "PeriodEndRef", "type": "int", "required": False, "description": None}]
        second_done = threading.Event()

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:2], "excludedFiles": []}
            if payload["sourceFiles"] == FILES[0:1]:
                # The first batch finishes last, but its conflicting field must still win.
                second_done.wait(timeout=5)
                return catalog(commands=[first])
            second_done.set()
            return catalog(commands=[second])

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o",
            generator_batch_size=1, generator_concurrency=2, invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual("string", result["catalogs"][0]["catalog"]["commands"][0]["fields"][0]["type"])


class SenderMergeTests(unittest.TestCase):
    def test_senders_from_several_batches_are_combined(self):
        from workflow import merge_catalogs
        def catalog(sender_file):
            return {"repository": "o/r", "ref": "main", "path": "src", "events": [], "commands": [
                {"name": "SendEmailCommand", "namespace": "SFA.DAS.Notifications.Messages.Commands", "sourceFile": sender_file,
                 "description": None, "fields": [], "handlers": [], "senders": [{"name": sender_file.split("/")[-1][:-3], "sourceFile": sender_file}]}]}
        merged, _ = merge_catalogs([catalog("src/B.cs"), catalog("src/A.cs"), catalog("src/B.cs")])
        [message] = merged["commands"]
        self.assertEqual(["src/A.cs", "src/B.cs"], [s["sourceFile"] for s in message["senders"]])


    def test_a_sent_only_entry_folds_into_the_declared_message_of_the_same_name(self):
        from workflow import merge_catalogs
        sent = {"repository": "o/r", "ref": "main", "path": "src", "events": [], "commands": [
            {"name": "SendEmailToEmployerCommand", "namespace": "SFA.DAS.Notifications.Messages.Commands", "sourceFile": "src/H.cs",
             "description": None, "fields": [], "handlers": [], "senders": [{"name": "H", "sourceFile": "src/H.cs"}]}]}
        declared = {"repository": "o/r", "ref": "main", "path": "src", "events": [], "commands": [
            {"name": "SendEmailToEmployerCommand", "namespace": "SFA.DAS.CommitmentsV2.Messages.Commands", "sourceFile": "src/Messages/C.cs",
             "description": None, "fields": [{"name": "To", "type": "string", "required": True, "description": None}],
             "handlers": [{"name": "SendEmailToEmployerCommandHandler", "sourceFile": "src/S.cs"}], "senders": []}]}
        merged, warnings = merge_catalogs([sent, declared])
        [message] = merged["commands"]
        self.assertEqual("SFA.DAS.CommitmentsV2.Messages.Commands", message["namespace"])
        self.assertEqual(["src/H.cs"], [s["sourceFile"] for s in message["senders"]])
        self.assertEqual("FoldedSentMessage", warnings[-1]["errorType"])


class TenantTests(unittest.TestCase):
    def test_tenant_is_passed_to_discovery_only(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            return catalog()

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True, "tenant": "tenant/atlas"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertEqual({"sourceUrl": SOURCE, "tenant": "tenant/atlas"}, calls[0][1])
        self.assertNotIn("tenant", calls[1][1])


if __name__ == "__main__":
    unittest.main()
