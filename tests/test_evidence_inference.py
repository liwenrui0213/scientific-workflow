from __future__ import annotations

import copy
import os
from pathlib import Path
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import atomic_write_json, load_json, record_digest
from tools.studyctl.models import EVIDENCE_SCHEMA_VERSION, ValidationError
from tools.studyctl.validation import errors_only, validate_study


class EvidenceInferenceTests(WorkflowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = self.initialize_approved_with_claim()
        self.manifest = self.successful_run(self.paths)

    def complete_draft(self) -> tuple[Path, dict[str, object]]:
        path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [self.manifest["run_id"]],
        )
        item = load_json(path)
        self.assertIsInstance(item, dict)
        item["addresses"]["question"] = "Does the exact recorded result equal four?"
        item["runs"][0]["role"] = "supporting"
        item["analysis"]["method"] = "Compare the exact recorded integer with four."
        item["result"] = {"value": 4, "comparison": "equal"}
        item["scope"] = "Only the deterministic fixture and recorded Cohort."
        item["uncertainty"] = "No sampling uncertainty is asserted."
        item["limitations"] = ["This does not establish a broader scientific result."]
        self.fill_evidence_inference(item)
        item["assessment"] = "supports"
        return path, item

    def test_new_draft_scaffolds_explicit_inference_placeholders(self) -> None:
        path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [self.manifest["run_id"]],
        )

        draft = load_json(path)

        self.assertEqual(draft["schema_version"], EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(
            draft["inference"],
            {
                "observation_to_claim": None,
                "auxiliary_assumptions": [],
                "competing_explanations": [],
                "falsification_conditions": [],
            },
        )

    def test_complete_inference_finalizes_and_revalidates(self) -> None:
        path, item = self.complete_draft()
        atomic_write_json(path, item)

        finalized = load_json(finalize_evidence(self.paths, path))

        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(
            finalized["inference"]["observation_to_claim"],
            item["inference"]["observation_to_claim"],
        )
        self.assertEqual(errors_only(validate_study(self.paths)), [])

    def test_each_inference_component_is_required_at_finalization(self) -> None:
        path, complete = self.complete_draft()
        invalid_cases = {
            "observation_to_claim": ("observation_to_claim", "   "),
            "auxiliary_assumptions": ("auxiliary_assumptions", []),
            "competing_explanations": ("competing_explanations", ["   "]),
            "falsification_conditions": ("falsification_conditions", []),
        }

        for expected, (field, invalid_value) in invalid_cases.items():
            with self.subTest(field=field):
                item = copy.deepcopy(complete)
                item["inference"][field] = invalid_value
                atomic_write_json(path, item)

                with self.assertRaisesRegex(ValidationError, expected):
                    finalize_evidence(self.paths, path)

                unchanged = load_json(path)
                self.assertEqual(unchanged["status"], "draft")
                self.assertIsNone(unchanged["record_sha256"])

    def test_legacy_v1_finalized_evidence_remains_valid_but_v1_draft_cannot_bypass(self) -> None:
        path, item = self.complete_draft()
        bypass = copy.deepcopy(item)
        bypass["schema_version"] = 1
        bypass.pop("inference")
        atomic_write_json(path, bypass)

        with self.assertRaisesRegex(ValidationError, "inference"):
            finalize_evidence(self.paths, path)

        upgraded = copy.deepcopy(item)
        atomic_write_json(path, upgraded)
        finalized = load_json(finalize_evidence(self.paths, path))
        legacy = copy.deepcopy(finalized)
        legacy["schema_version"] = 1
        legacy.pop("inference")
        legacy["record_sha256"] = record_digest(legacy, "record_sha256")
        os.chmod(path, 0o644)
        atomic_write_json(path, legacy)
        os.chmod(path, 0o444)

        self.assertEqual(errors_only(validate_study(self.paths)), [])


if __name__ == "__main__":
    unittest.main()
