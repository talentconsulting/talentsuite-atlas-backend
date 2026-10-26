import threading
import unittest

from workflow import WorkflowError, catalog_to_puml, merge_catalogs, parse_workflow_request, run_workflow


SOURCE = "https://github.com/source/app/tree/main/src"
FILES = [f"https://github.com/source/app/blob/main/src/Clients/Client{index}.cs" for index in range(2)]


def container(container_id="app"):
    return {"id": container_id, "name": "App", "type": "api", "evidence": []}


def dependency(name="Accounts API", source_id="app"):
    return {
        "sourceId": source_id,
        "name": name,
        "kind": "http-api",
        "classification": "unknown",
        "direction": "outbound",
        "client": "AccountsClient",
        "technology": "HttpClient",
        "configurationKeys": ["AccountsApi:BaseUrl"],
        "authentication": {"type": None, "configurationKeys": []},
        "operations": [],
        "resources": [],
        "evidence": [{"sourceFile": "src/Clients/Client0.cs", "reason": "HTTP client"}],
        "confidence": "medium",
        "targetId": f"http-api-{name.lower().replace(' ', '-')}",
        "description": "Calls the API of",
    }


def catalog(items=None, containers=None):
    return {
        "repository": "source/app", "ref": "main", "path": "src",
        "systemName": "App", "containers": containers if containers is not None else [container()],
        "dependencies": items or [],
    }


class WorkflowTests(unittest.TestCase):
    def test_direct_request_requires_target_repository(self):
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request('{"sourceUrl":"%s"}' % SOURCE)
        self.assertTrue(parse_workflow_request('{"sourceUrl":"%s","deferPublication":true}' % SOURCE)["deferPublication"])

    def test_deferred_workflow_batches_and_merges(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            if name == "generator":
                item = dependency("Accounts API")
                item["evidence"] = [{"sourceFile": payload["sourceFiles"][0].split("/blob/main/")[1], "reason": "HTTP client"}]
                return catalog([item])
            raise AssertionError(name)

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", generator_batch_size=1, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, len(result["catalogs"][0]["catalog"]["dependencies"]))
        self.assertEqual(2, len(result["catalogs"][0]["catalog"]["dependencies"][0]["evidence"]))

    def test_merge_enriches_unknown_values_and_unions_evidence(self):
        first = dependency()
        second = dependency()
        second["classification"] = "internal"
        second["authentication"] = {"type": "oauth2", "configurationKeys": ["AccountsApi:Identifier"]}
        second["evidence"] = [{"sourceFile": "src/Startup.cs", "reason": "Typed client registration"}]
        merged = merge_catalogs([catalog([first]), catalog([second])])["dependencies"][0]
        self.assertEqual("internal", merged["classification"])
        self.assertEqual("oauth2", merged["authentication"]["type"])
        self.assertEqual(2, len(merged["evidence"]))

    def test_merge_keeps_first_scalar_when_batches_format_api_metadata_differently(self):
        first = dependency("ApprovalsOuterApiClient")
        first["authentication"] = {"type": "API Key", "configurationKeys": ["ApprovalsApi:Key"]}
        second = dependency("ApprovalsOuterApiClient")
        second["technology"] = ".NET HttpClient"
        second["authentication"] = {"type": "api-key", "configurationKeys": ["ApprovalsApi.Key"]}
        second["evidence"] = [{"sourceFile": "src/Startup.cs", "reason": "Typed client registration"}]

        merged = merge_catalogs([catalog([first]), catalog([second])])["dependencies"][0]

        self.assertEqual("HttpClient", merged["technology"])
        self.assertEqual("API Key", merged["authentication"]["type"])
        self.assertEqual(["ApprovalsApi.Key", "ApprovalsApi:Key"], merged["authentication"]["configurationKeys"])
        self.assertEqual(2, len(merged["evidence"]))

    def test_merge_collapses_client_and_api_suffix_variants(self):
        first = dependency("ReservationsApiClient")
        first["evidence"] = [{"sourceFile": "src/Clients/ReservationsApiClient.cs", "reason": "Client construction"}]
        second = dependency("ReservationsApi")
        second["evidence"] = [{"sourceFile": "src/Startup.cs", "reason": "Registered as ReservationsApi"}]
        third = dependency("IReservationsApiClient")
        third["evidence"] = [{"sourceFile": "src/Consumers/OrderConsumer.cs", "reason": "Interface usage"}]

        merged = merge_catalogs([catalog([first]), catalog([second]), catalog([third])])

        self.assertEqual(1, len(merged["dependencies"]))
        self.assertEqual(3, len(merged["dependencies"][0]["evidence"]))

    def test_merge_collapses_dbcontext_name_into_the_resource_named_entry(self):
        resource_named = dependency("ProviderCommitments")
        resource_named["kind"] = "database"
        resource_named["evidence"] = [{"sourceFile": "src/Startup.cs", "reason": "Connection string registration"}]
        dbcontext_named = dependency("ProviderCommitmentsDbContext")
        dbcontext_named["kind"] = "database"
        dbcontext_named["evidence"] = [{"sourceFile": "src/Data/ProviderCommitmentsDbContext.cs", "reason": "DbContext subclass"}]

        merged = merge_catalogs([catalog([resource_named]), catalog([dbcontext_named])])

        self.assertEqual(1, len(merged["dependencies"]))
        self.assertEqual("ProviderCommitments", merged["dependencies"][0]["name"])
        self.assertEqual(2, len(merged["dependencies"][0]["evidence"]))

    def test_merge_collapses_all_database_names_within_one_container_regardless_of_similarity(self):
        # There is reliably at most one database per repository, so even completely unrelated-looking
        # names for the same container's database dependency must collapse into one entry.
        first = dependency("SQL Server")
        first["kind"] = "database"
        second = dependency("Commitments Database")
        second["kind"] = "database"

        merged = merge_catalogs([catalog([first]), catalog([second])])

        self.assertEqual(1, len(merged["dependencies"]))
        self.assertEqual("SQL Server", merged["dependencies"][0]["name"])

    def test_merge_synchronizes_database_name_across_different_containers(self):
        containers = [container("api"), {"id": "jobs", "name": "App Jobs", "type": "job", "evidence": []}]
        api_db = dependency("SQL Server", source_id="api")
        api_db["kind"] = "database"
        jobs_db = dependency("ProviderCommitmentsDb", source_id="jobs")
        jobs_db["kind"] = "database"

        merged = merge_catalogs([catalog([api_db, jobs_db], containers=containers)])

        self.assertEqual(2, len(merged["dependencies"]))
        names = {item["name"] for item in merged["dependencies"]}
        target_ids = {item["targetId"] for item in merged["dependencies"]}
        source_ids = {item["sourceId"] for item in merged["dependencies"]}
        self.assertEqual(1, len(names))
        self.assertEqual(1, len(target_ids))
        self.assertEqual({"api", "jobs"}, source_ids)

    def test_merge_collapses_containers_differing_only_by_version_token_and_casing(self):
        first_catalog = catalog(
            [dependency(source_id="jobs-v2")],
            containers=[{"id": "jobs-v2", "name": "CommitmentsV2 Jobs", "type": "job", "evidence": []}],
        )
        second_catalog = catalog(
            [dependency(name="Other Dependency", source_id="jobs")],
            containers=[{"id": "jobs", "name": "Commitments Jobs", "type": "job", "evidence": []}],
        )

        merged = merge_catalogs([first_catalog, second_catalog])

        self.assertEqual(1, len(merged["containers"]))
        canonical_id = merged["containers"][0]["id"]
        self.assertTrue(all(item["sourceId"] == canonical_id for item in merged["dependencies"]))

    def test_merge_disambiguates_the_same_id_reused_for_two_different_containers(self):
        # Two different batches each independently picked id "app" for two GENUINELY different
        # containers (a job and an api) -- they must not collide in the merged result, since that
        # would make dependency sourceId references ambiguous.
        job_catalog = catalog(
            [dependency(source_id="app")],
            containers=[{"id": "app", "name": "App", "type": "job", "evidence": []}],
        )
        api_catalog = catalog(
            [dependency(name="Other Dependency", source_id="app")],
            containers=[{"id": "app", "name": "App", "type": "api", "evidence": []}],
        )

        merged = merge_catalogs([job_catalog, api_catalog])

        container_ids = [item["id"] for item in merged["containers"]]
        self.assertEqual(2, len(container_ids))
        self.assertEqual(len(container_ids), len(set(container_ids)))
        dependency_source_ids = {item["sourceId"] for item in merged["dependencies"]}
        self.assertEqual(set(container_ids), dependency_source_ids)

    def test_merge_reconciles_container_ids_across_batches(self):
        first_catalog = catalog([dependency(source_id="api-batch-1")], containers=[container("api-batch-1")])
        second_catalog = catalog(
            [dependency(name="Reservations API", source_id="api-batch-2")], containers=[container("api-batch-2")]
        )

        merged = merge_catalogs([first_catalog, second_catalog])

        self.assertEqual(1, len(merged["containers"]))
        canonical_id = merged["containers"][0]["id"]
        self.assertEqual(2, len(merged["dependencies"]))
        self.assertTrue(all(item["sourceId"] == canonical_id for item in merged["dependencies"]))

    def test_merge_keeps_dependencies_from_different_containers_separate(self):
        containers = [container("api"), {"id": "jobs", "name": "App Jobs", "type": "job", "evidence": []}]
        merged = merge_catalogs([catalog(
            [dependency(source_id="api"), dependency(source_id="jobs")], containers=containers,
        )])

        self.assertEqual(2, len(merged["dependencies"]))
        self.assertEqual({"api", "jobs"}, {item["sourceId"] for item in merged["dependencies"]})

    def test_generator_batches_run_concurrently(self):
        files = [f"https://github.com/source/app/blob/main/src/Clients/Client{index}.cs" for index in range(4)]
        barrier = threading.Barrier(4, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": files, "excludedFiles": []}
            if name == "generator":
                # Only releases once all 4 batches have reached this point at the same time --
                # if batches ran one at a time, this would time out and break the barrier.
                barrier.wait()
                return catalog([dependency(name=payload["sourceFiles"][0])])
            raise AssertionError(name)

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o",
            generator_batch_size=1, max_concurrency=4, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(4, len(result["catalogs"][0]["catalog"]["dependencies"]))

    def test_failed_batch_fails_closed(self):
        publisher_called = False

        def invoke(project, name, model, payload, max_attempts=2):
            nonlocal publisher_called
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            if name == "publisher":
                publisher_called = True
            if payload["sourceFiles"] == FILES[1:]:
                return {"error": {"code": "invalid_model_output", "message": "bad catalog"}}
            return catalog([dependency()])

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", generator_batch_size=1, invoker=invoke,
        )
        self.assertFalse(result["success"])
        self.assertEqual("partial_generation_failed", result["errors"][0]["code"])
        self.assertEqual([], result["catalogs"])
        self.assertFalse(publisher_called)

    def test_discovery_finding_no_files_succeeds_with_an_empty_catalog(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": [], "excludedFiles": []}
            raise AssertionError("The generator must not be called when discovery finds nothing.")

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(0, result["discoveredFileCount"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["dependencies"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["containers"])
        self.assertEqual("app", result["catalogs"][0]["catalog"]["systemName"])

    def test_a_legitimately_empty_generated_catalog_still_succeeds(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            return catalog()

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["dependencies"])

    def test_direct_workflow_publishes_deterministic_path(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            if name == "generator":
                return catalog([dependency()])
            self.assertEqual("app/service-dependencies/service-dependencies.json", payload["catalogs"][0]["targetPath"])
            self.assertIn("@startuml", payload["catalogs"][0]["puml"])
            return {"success": True, "status": "created"}

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "targetRepository": "target/catalog"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertTrue(result["success"])

    def test_deferred_workflow_returns_a_puml_diagram_alongside_the_catalog(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            return catalog([dependency()])

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertIn("@startuml", result["catalogs"][0]["puml"])


class TenantTests(unittest.TestCase):
    def invoke_workflow(self, **request):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            return catalog([dependency()])

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True, **request},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        return calls

    def test_tenant_is_passed_to_discovery_and_the_generator(self):
        calls = self.invoke_workflow(tenant="tenant/atlas")
        self.assertEqual({"sourceUrl": SOURCE, "tenant": "tenant/atlas"}, calls[0][1])
        self.assertEqual({"sourceUrl": SOURCE, "sourceFiles": FILES[:1], "tenant": "tenant/atlas"}, calls[1][1])

    def test_without_a_tenant_no_tenant_is_sent(self):
        calls = self.invoke_workflow()
        self.assertEqual({"sourceUrl": SOURCE}, calls[0][1])
        self.assertEqual({"sourceUrl": SOURCE, "sourceFiles": FILES[:1]}, calls[1][1])


class CatalogToPumlTests(unittest.TestCase):
    def test_declares_each_container_and_dependency(self):
        doc = catalog_to_puml(catalog([dependency()]))
        self.assertIn('Container(c_app, "App", "API")', doc)
        self.assertIn('System_Ext(d_http_api_accounts_api, "Accounts API", "HttpClient")', doc)
        self.assertIn('Rel(c_app, d_http_api_accounts_api, "Calls the API of", "HttpClient")', doc)
        self.assertTrue(doc.startswith("@startuml\n"))
        self.assertTrue(doc.endswith("@enduml\n"))

    def test_rows_sharing_a_target_id_declare_one_node_and_multiple_edges(self):
        containers = [container("api"), {"id": "jobs", "name": "App Jobs", "type": "job", "evidence": []}]
        first = dependency(source_id="api")
        second = dependency(source_id="jobs")
        second["targetId"] = first["targetId"]
        doc = catalog_to_puml(catalog([first, second], containers=containers))
        self.assertEqual(1, doc.count("System_Ext(d_http_api_accounts_api"))
        self.assertIn("Rel(c_api, d_http_api_accounts_api", doc)
        self.assertIn("Rel(c_jobs, d_http_api_accounts_api", doc)

    def test_hyphenated_ids_are_sanitized_into_valid_plantuml_aliases(self):
        doc = catalog_to_puml(catalog(
            [dependency(source_id="api-batch-1")], containers=[container("api-batch-1")],
        ))
        self.assertIn("Container(c_api_batch_1,", doc)
        self.assertIn("d_http_api_accounts_api", doc)

    def test_database_and_message_broker_use_their_own_c4_macros(self):
        db = dependency("Provider Commitments Database")
        db["kind"] = "database"
        db["targetId"] = "database-provider-commitments-database"
        queue = dependency("NServiceBus")
        queue["kind"] = "message-broker"
        queue["targetId"] = "message-broker-nservicebus"
        doc = catalog_to_puml(catalog([db, queue]))
        self.assertIn('SystemDb_Ext(d_database_provider_commitments_database, "Provider Commitments Database"', doc)
        self.assertIn('SystemQueue_Ext(d_message_broker_nservicebus, "NServiceBus"', doc)


if __name__ == "__main__":
    unittest.main()
