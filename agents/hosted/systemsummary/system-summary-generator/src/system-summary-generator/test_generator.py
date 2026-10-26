import json
import unittest

from generator import GenerationError, generate_from_text, parse_input, source_prompt, validate_summary


REPOSITORY = "source/catalog"
SUMMARY = {
    "repository": REPOSITORY,
    "name": "Catalog",
    "description": "Manages product catalog entries and their pricing.",
    "domain": "Catalog management",
    "capabilities": ["Product catalog", "Pricing"],
    "confidence": "high",
}


def payload(**overrides):
    base = {
        "repository": REPOSITORY,
        "database": {"tables": [{"name": "Products"}]},
        "events": None,
        "dependencies": None,
        "apiControllers": ["ProductsController"],
    }
    base.update(overrides)
    return base


class InputTests(unittest.TestCase):
    def test_accepts_a_well_formed_payload(self):
        result = parse_input(json.dumps(payload()))
        self.assertEqual(REPOSITORY, result["repository"])

    def test_rejects_extra_fields(self):
        raw = json.dumps(payload())
        with_extra = json.loads(raw)
        with_extra["extra"] = True
        with self.assertRaises(GenerationError):
            parse_input(json.dumps(with_extra))

    def test_accepts_a_tenant_and_keeps_it_out_of_the_prompt(self):
        result = parse_input(json.dumps(payload(tenant="org/catalogue")))
        self.assertNotIn("tenant", result)
        self.assertNotIn("org/catalogue", source_prompt(result))

    def test_rejects_an_invalid_tenant(self):
        with self.assertRaisesRegex(GenerationError, "tenant"):
            parse_input(json.dumps(payload(tenant="not-a-repository")))

    def test_rejects_an_invalid_repository(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps(payload(repository="not-a-repository")))

    def test_requires_at_least_one_piece_of_evidence(self):
        with self.assertRaisesRegex(GenerationError, "must be supplied"):
            parse_input(json.dumps(payload(database=None, apiControllers=[])))

    def test_rejects_non_object_catalogs(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps(payload(database=["not", "an", "object"])))


class SummaryValidationTests(unittest.TestCase):
    def test_validates_a_complete_summary(self):
        self.assertEqual(SUMMARY, validate_summary(SUMMARY, REPOSITORY))

    def test_rejects_a_mismatched_repository(self):
        with self.assertRaisesRegex(GenerationError, "does not match"):
            validate_summary(SUMMARY, "other/repository")

    def test_rejects_missing_fields(self):
        incomplete = json.loads(json.dumps(SUMMARY))
        del incomplete["domain"]
        with self.assertRaisesRegex(GenerationError, "exactly"):
            validate_summary(incomplete, REPOSITORY)

    def test_rejects_too_many_capabilities(self):
        too_many = json.loads(json.dumps(SUMMARY))
        too_many["capabilities"] = [f"Capability {index}" for index in range(9)]
        with self.assertRaisesRegex(GenerationError, "at most"):
            validate_summary(too_many, REPOSITORY)

    def test_rejects_an_invalid_confidence(self):
        invalid = json.loads(json.dumps(SUMMARY))
        invalid["confidence"] = "certain"
        with self.assertRaisesRegex(GenerationError, "confidence"):
            validate_summary(invalid, REPOSITORY)

    def test_allows_a_null_domain(self):
        sparse = json.loads(json.dumps(SUMMARY))
        sparse["domain"] = None
        self.assertIsNone(validate_summary(sparse, REPOSITORY)["domain"])


class GenerateFromTextTests(unittest.TestCase):
    def test_generates_a_summary_from_the_supplied_catalogs(self):
        prompts = []

        def completion(prompt):
            prompts.append(prompt)
            return json.dumps(SUMMARY)

        result = generate_from_text(json.dumps(payload()), completion=completion)

        self.assertEqual(SUMMARY, result)
        self.assertIn("ProductsController", prompts[0])

    def test_rejects_non_json_model_output(self):
        with self.assertRaisesRegex(GenerationError, "valid JSON"):
            generate_from_text(json.dumps(payload()), completion=lambda prompt: "not-json")


if __name__ == "__main__":
    unittest.main()
