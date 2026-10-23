import json
import unittest
from unittest import mock

import generator
from generator import GenerationError, generate_from_text


SOURCE = "https://github.com/owner/app/tree/main/src"
BLOB = "https://github.com/owner/app/blob/main/src/App.Web/Controllers/ApplyController.cs"
PAGES = [{"id": p} for p in ["Vacancy", "Apply", "Jobs", "Qualifications", "Preview", "Submitted"]]
TRANSITIONS = [
    {"from": "Vacancy", "to": "Apply", "trigger": "link"},
    {"from": "Apply", "to": "Jobs", "trigger": "link"},
    {"from": "Apply", "to": "Qualifications", "trigger": "link"},
    {"from": "Jobs", "to": "Apply", "trigger": "redirect"},
    {"from": "Apply", "to": "Preview", "trigger": "link"},
    {"from": "Preview", "to": "Submitted", "trigger": "form-post"},
]


def describe_input(**overrides):
    return json.dumps({"mode": "describe", "sourceUrl": SOURCE, "pages": PAGES[:2], "transitions": TRANSITIONS[:1],
                       "sourceFiles": [BLOB], **overrides})


def journeys_input():
    return json.dumps({"mode": "journeys", "repository": "owner/app", "pages": PAGES, "transitions": TRANSITIONS})


def journey(**overrides):
    return {"name": "Apply", "description": "Apply for a job.", "kind": "linear", "steps": ["Vacancy", "Apply", "Preview"],
            "hub": None, "sections": [], "then": [], **overrides}


def loader(location, paths, tenant=None):
    return {path: "public IActionResult Apply() {}" for path in paths}


class DescribeTests(unittest.TestCase):
    def run_model(self, output, **overrides):
        return generate_from_text(describe_input(**overrides), completion=lambda i, p: json.dumps(output), source_loader=loader)

    def test_returns_purposes_and_conditions_for_supplied_items_only(self):
        result = self.run_model({
            "pages": [{"id": "Vacancy", "purpose": "Shows a vacancy."}, {"id": "Invented", "purpose": "x"}],
            "transitions": [{"from": "Vacancy", "to": "Apply", "trigger": "link", "condition": "when the vacancy is open"},
                            {"from": "Apply", "to": "Invented", "trigger": "link", "condition": "x"}],
        })
        self.assertEqual([{"id": "Apply", "purpose": None, "fields": []}, {"id": "Vacancy", "purpose": "Shows a vacancy.", "fields": []}], result["pages"])
        self.assertEqual([{"from": "Vacancy", "to": "Apply", "trigger": "link", "condition": "when the vacancy is open"}],
                         result["transitions"])

    def test_describes_validation_for_supplied_fields_only(self):
        pages = [{"id": "Vacancy", "fields": [{"name": "Postcode", "control": "text"}]}, {"id": "Apply"}]
        result = generate_from_text(describe_input(pages=pages), completion=lambda i, p: json.dumps({
            "pages": [{"id": "Vacancy", "purpose": "x", "fields": [{"name": "Postcode", "validation": "required; valid UK postcode"},
                                                                    {"name": "Invented", "validation": "x"}]}],
            "transitions": []}), source_loader=loader)
        self.assertEqual([{"name": "Postcode", "validation": "required; valid UK postcode"}], result["pages"][1]["fields"])
        self.assertEqual([], result["pages"][0]["fields"])

    def test_unconditional_descriptions_become_null(self):
        result = self.run_model({"pages": [], "transitions": [
            {"from": "Vacancy", "to": "Apply", "trigger": "link", "condition": "Ordinary link."}]})
        self.assertIsNone(result["transitions"][0]["condition"])

    def test_sends_files_and_graph_to_the_model(self):
        seen = {}
        generate_from_text(describe_input(), completion=lambda i, p: seen.setdefault("prompt", p) and '{"pages":[],"transitions":[]}',
                           source_loader=loader)
        self.assertIn("ApplyController.cs", seen["prompt"])
        self.assertIn('"Vacancy"', seen["prompt"])

    def test_rejects_files_from_another_repository(self):
        with self.assertRaisesRegex(GenerationError, "same|repository and ref"):
            generate_from_text(describe_input(sourceFiles=["https://github.com/other/app/blob/main/x.cs"]),
                               completion=lambda i, p: "{}", source_loader=loader)

    def test_rejects_output_with_the_wrong_shape(self):
        with self.assertRaisesRegex(GenerationError, "exactly pages and transitions"):
            self.run_model({"pages": []})

    def test_rejects_unknown_modes(self):
        with self.assertRaisesRegex(GenerationError, "mode"):
            generate_from_text(json.dumps({"mode": "other"}), completion=lambda i, p: "{}", source_loader=loader)


class JourneyTests(unittest.TestCase):
    def run_model(self, journeys, repaired=()):
        answers = iter([{"journeys": journeys}, {"journeys": list(repaired)}])   # first call, then the repair round
        return generate_from_text(journeys_input(), completion=lambda i, p: json.dumps(next(answers)), source_loader=loader)

    def test_a_repaired_journey_replaces_its_warning(self):
        result = self.run_model([journey(steps=["Vacancy", "Jobs", "Preview"])], repaired=[journey()])
        self.assertEqual(["Apply"], [j["name"] for j in result["journeys"]])
        self.assertEqual([], result["warnings"])

    def test_warnings_name_the_missing_transition(self):
        result = self.run_model([journey(steps=["Vacancy", "Jobs", "Preview"])])
        self.assertIn("Vacancy -> Jobs", result["warnings"][0])

    def test_keeps_linear_journeys_that_follow_transitions(self):
        result = self.run_model([journey()])
        self.assertEqual(["Vacancy", "Apply", "Preview"], result["journeys"][0]["steps"])
        self.assertEqual([], result["warnings"])

    def test_keeps_hub_journeys(self):
        result = self.run_model([journey(kind="hub", steps=["Vacancy", "Apply"], hub="Apply",
                                         sections=["Jobs", "Qualifications"], then=["Preview", "Submitted"])])
        self.assertEqual("Apply", result["journeys"][0]["hub"])

    def test_drops_journeys_with_invented_steps(self):
        result = self.run_model([
            journey(steps=["Vacancy", "Jobs", "Preview"]),                    # Vacancy -> Jobs is not a transition
            journey(name="Other", steps=["Vacancy", "Apply", "Nowhere"]),     # unknown page
            journey(name="Hub", kind="hub", steps=["Apply"], hub="Apply", sections=["Jobs", "Submitted"], then=[]),
        ])
        self.assertEqual([], result["journeys"])
        self.assertEqual(3, len(result["warnings"]))

    def test_drops_trivial_and_duplicate_journeys(self):
        result = self.run_model([journey(steps=["Vacancy", "Apply"]), journey(), journey(name="apply")])
        self.assertEqual(1, len(result["journeys"]))
        self.assertEqual(2, len(result["warnings"]))


class TenantTests(unittest.TestCase):
    def test_describe_passes_the_tenant_to_the_source_loader(self):
        seen = []

        def tenant_loader(location, paths, tenant=None):
            seen.append(tenant)
            return loader(location, paths)

        generate_from_text(describe_input(tenant="tenant/atlas"), completion=lambda i, p: '{"pages":[],"transitions":[]}',
                           source_loader=tenant_loader)
        self.assertEqual(["tenant/atlas"], seen)

    def test_rejects_a_tenant_that_is_not_a_string(self):
        with self.assertRaisesRegex(GenerationError, "tenant"):
            generate_from_text(describe_input(tenant=1), completion=lambda i, p: "{}", source_loader=loader)


class SourceDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"class Apply {}"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(generator.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(generator.urllib.request, "urlopen", side_effect=urlopen):
            generator._download(generator.parse_source_url(SOURCE), ["src/App.cs"], "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_reads_through_the_contents_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/owner/app/contents/src/App.cs?ref=main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))
        self.assertEqual("application/vnd.github.raw", request.get_header("Accept"))

    def test_without_a_token_the_raw_file_is_read(self):
        request = self.download(None)
        self.assertEqual("https://raw.githubusercontent.com/owner/app/main/src/App.cs", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
