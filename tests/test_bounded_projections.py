from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import (
    active_selector_bytes,
    compaction_pressure,
    require_growth_allowed,
    runtime_compaction_due_path,
    write_active_selector,
)
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.graph_records import (
    GRAPH_RECORD_LOCATOR_BUDGET_BYTES,
    _bounded_graph_record_projection,
)
from tools.studyctl.hashing import (
    atomic_write_json,
    canonical_json_bytes,
    load_json,
    sha256_file,
    sha256_json,
)
from tools.studyctl.models import StudyPaths, ValidationError, utc_now
from tools.studyctl.rendering import render_status
from tools.studyctl.review import create_review_packet
from tools.studyctl.validation import (
    load_schema,
    object_schema_issues,
    validate_schema_instance,
)


class BoundedProjectionTests(WorkflowTestCase):
    @staticmethod
    def graph_sequence_locator() -> dict[str, object]:
        return {
            "path": "studies/SC-0001/GRAPH_RECORDS.sequence.json",
            "size": 512,
            "file_sha256": "a" * 64,
            "high_water_mark": 0,
            "inventory_sha256": sha256_json([]),
        }

    def set_run_pressure(self, *, soft: int, hard: int) -> None:
        policy_path = self.root / "scientific-workflow" / "policy.json"
        policy = load_json(policy_path)
        policy["active_context"]["compaction_pressure"][
            "runs_since_checkpoint"
        ] = {"soft": soft, "hard": hard}
        atomic_write_json(policy_path, policy)

    def finalized_other_evidence(
        self, paths: StudyPaths, marker: str
    ) -> dict[str, object]:
        manifest = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        draft = load_json(draft_path)
        draft["addresses"]["question"] = "Does the recorded Run provide context?"
        draft["analysis"]["method"] = "Deterministic source-index fixture."
        draft["result"] = {"bounded_detail": marker}
        draft["scope"] = "Only the deterministic fixture."
        draft["uncertainty"] = "No sampling uncertainty is claimed."
        self.fill_evidence_inference(draft)
        draft["assessment"] = "inconclusive"
        atomic_write_json(draft_path, draft)
        finalized = load_json(finalize_evidence(paths, draft_path))

        claims = load_json(paths.claims)
        claims["claims"][0]["other_evidence"] = [
            {
                "evidence_id": finalized["evidence_id"],
                "version": finalized["version"],
                "sha256": finalized["record_sha256"],
            }
        ]
        claims["revision"] += 1
        atomic_write_json(paths.claims, claims)
        return finalized

    def set_maximal_valid_frontier(self, paths: StudyPaths) -> dict[str, object]:
        claims = load_json(paths.claims)
        claim_records = []
        claim_ids = []
        for index in range(64):
            claim_id = f"CLAIM-{index + 1:04d}"
            claim_ids.append(claim_id)
            claim_records.append(
                {
                    "claim_id": claim_id,
                    "statement": "s" * 4060 + f"-STATEMENT-TAIL-{index:02d}",
                    "scope": "c" * 4060 + f"-SCOPE-TAIL-{index:02d}",
                    "state": "proposed",
                    "evidence_basis": "none",
                    "lifecycle": "active",
                    "supporting_evidence": [],
                    "contradictory_evidence": [],
                    "other_evidence": [],
                    "uncertainty": "u" * 4096,
                    "limitations": ["l" * 1024 for _ in range(32)],
                    "updated_at": utc_now(),
                }
            )
        claims["claims"] = claim_records
        claims["frontier"] = {
            "summary": "m" * 4096,
            "claim_ids": claim_ids,
            "open_questions": [
                "q" * 1010 + f"-QUESTION-{index:02d}" for index in range(64)
            ],
            "human_decisions_required": [
                "d" * 1010 + f"-DECISION-{index:02d}" for index in range(32)
            ],
        }
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        return claims

    @staticmethod
    def graph_locator_fixture(
        *, current_count: int, versions: int
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        if current_count < 1 or versions < 1:
            raise ValueError("current_count and versions must both be positive")
        intent_history: list[dict[str, object]] = []
        intent_items: list[dict[str, object]] = []
        plan_history: list[dict[str, object]] = []
        plan_items: list[dict[str, object]] = []
        draft_items: list[dict[str, object]] = []
        for index in range(1, current_count + 1):
            intent_id = f"INTENT-{index:04d}"
            control_graph_id = f"CG-{index:04d}"
            for version in range(1, versions + 1):
                intent_digest = f"{index * 10_000 + version:064x}"
                intent = {
                    "intent_id": intent_id,
                    "version": version,
                    "sha256": intent_digest,
                    "path": (
                        f"studies/SC-0001/intents/{intent_id}.v{version:04d}.json"
                    ),
                    "size": 4096,
                }
                intent_history.append(intent)
                plan = {
                    "control_graph_id": control_graph_id,
                    "version": version,
                    "sha256": f"{1_000_000 + index * 10_000 + version:064x}",
                    "realizes_intent": {
                        "intent_id": intent_id,
                        "version": version,
                        "sha256": intent_digest,
                    },
                    "path": (
                        "studies/SC-0001/control-plans/"
                        f"{control_graph_id}.v{version:04d}.json"
                    ),
                    "size": 4096,
                }
                plan_history.append(plan)
            intent_items.append(intent_history[-1])
            plan_items.append(plan_history[-1])
            draft_items.append(
                {
                    "kind": "experiment_intent",
                    "id": intent_id,
                    "version": versions + 1,
                    "path": (
                        "studies/SC-0001/work/active/"
                        f"{intent_id}.v{versions + 1:04d}."
                        "experiment-intent.draft.json"
                    ),
                    "size": 4096,
                    "file_sha256": f"{2_000_000 + index:064x}",
                    "assurance": "mutable_non_authoritative",
                }
            )
        return (
            intent_history,
            intent_items,
            plan_history,
            plan_items,
            draft_items,
        )

    def test_active_selector_indexes_large_sources_without_embedding_them(self) -> None:
        paths = self.initialize_approved_with_claim()
        brief_marker = "BRIEF-CONTENT-MUST-NOT-BE-EMBEDDED-" + "b" * 180_000
        paths.brief.write_text(
            paths.brief.read_text(encoding="utf-8") + brief_marker,
            encoding="utf-8",
        )
        formal_marker = "FORMAL-CONTENT-MUST-NOT-BE-EMBEDDED-" + "f" * 180_000
        formal_path = paths.formal / "MODEL.md"
        formal_path.write_text(
            "status: active\n\n" + formal_marker,
            encoding="utf-8",
        )

        selector_path = write_active_selector(paths)
        selector = load_json(selector_path)
        serialized = selector_path.read_text(encoding="utf-8")

        self.assertNotIn(brief_marker, serialized)
        self.assertNotIn(formal_marker, serialized)
        self.assertEqual(selector["brief"]["size"], paths.brief.stat().st_size)
        formal_sources = selector["active_formal_artifacts"]["sources"]
        self.assertEqual(formal_sources[0]["size"], formal_path.stat().st_size)
        self.assertEqual(
            [item["claim_id"] for item in selector["selected_claims"]],
            ["CLAIM-0001"],
        )
        self.assertEqual(selector_path.stat().st_size, active_selector_bytes(selector))
        selector_metric = next(
            item
            for item in compaction_pressure(paths)["metrics"]
            if item["name"] == "active_selector_bytes"
        )
        self.assertEqual(selector_metric["observed"], selector_path.stat().st_size)
        self.assertLess(selector_path.stat().st_size, 32_000)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = studyctl_main(
                ["--root", str(self.root), "context", paths.study_id]
            )
        self.assertEqual(result, 0)
        self.assertEqual(Path(stdout.getvalue().strip()), selector_path)

    def test_maximal_valid_frontier_still_produces_a_bounded_selector(self) -> None:
        paths = self.initialize_approved_with_claim()
        claims = self.set_maximal_valid_frontier(paths)

        issues = object_schema_issues(paths.root, "claims", paths.claims, claims)
        self.assertEqual([issue.render() for issue in issues], [])

        selector_path = write_active_selector(paths)
        selector = load_json(selector_path)
        serialized = selector_path.read_text(encoding="utf-8")

        self.assertLess(selector_path.stat().st_size, 98_304)
        self.assertEqual(len(selector["selected_claims"]), 64)
        self.assertNotIn("STATEMENT-TAIL-63", serialized)
        self.assertNotIn("SCOPE-TAIL-63", serialized)
        self.assertNotIn("QUESTION-63", serialized)
        selected = selector["selected_claims"][-1]
        self.assertTrue(selected["statement"]["truncated"])
        self.assertEqual(selected["limitations_count"], 32)
        self.assertEqual(len(selected["sha256"]), 64)

        for label, fill in (("unicode", chr(0x1F600)), ("control", chr(1))):
            with self.subTest(serialization=label):
                for claim in claims["claims"]:
                    claim["statement"] = fill * 4096
                    claim["scope"] = fill * 4096
                    claim["uncertainty"] = fill * 4096
                    claim["limitations"] = [fill * 1024 for _ in range(32)]
                claims["frontier"]["summary"] = fill * 4096
                claims["frontier"]["open_questions"] = [fill * 1024 for _ in range(64)]
                claims["frontier"]["human_decisions_required"] = [
                    fill * 1024 for _ in range(32)
                ]
                claims["revision"] += 1
                claims["updated_at"] = utc_now()
                atomic_write_json(paths.claims, claims)

                issues = object_schema_issues(
                    paths.root, "claims", paths.claims, claims
                )
                self.assertEqual([issue.render() for issue in issues], [])
                selector_path = write_active_selector(paths)
                selector = load_json(selector_path)
                self.assertLess(selector_path.stat().st_size, 98_304)
                self.assertTrue(
                    all(
                        item[field]["preview_canonical_bytes"] <= 256
                        for item in selector["selected_claims"]
                        for field in ("statement", "scope")
                    )
                )

    def test_small_graph_locator_inventories_remain_complete(self) -> None:
        (
            intent_history,
            intent_items,
            plan_history,
            plan_items,
            draft_items,
        ) = self.graph_locator_fixture(current_count=1, versions=1)

        projection = _bounded_graph_record_projection(
            sequence_locator=self.graph_sequence_locator(),
            intent_history=intent_history,
            intent_items=intent_items,
            plan_history=plan_history,
            plan_items=plan_items,
            draft_items=draft_items,
        )

        for name in ("experiment_intents", "control_graphs", "workspace_drafts"):
            with self.subTest(index=name):
                index = projection[name]
                self.assertEqual(index["selected_count"], 1)
                self.assertEqual(len(index["items"]), 1)
                self.assertFalse(index["truncated"])
        self.assertLessEqual(
            len(canonical_json_bytes(projection)),
            GRAPH_RECORD_LOCATOR_BUDGET_BYTES,
        )

    def test_maximal_frontier_and_large_graph_inventories_fit_active_context(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        claims = self.set_maximal_valid_frontier(paths)
        self.assertEqual(
            [
                issue.render()
                for issue in object_schema_issues(
                    paths.root, "claims", paths.claims, claims
                )
            ],
            [],
        )
        (
            intent_history,
            intent_items,
            plan_history,
            plan_items,
            draft_items,
        ) = self.graph_locator_fixture(current_count=64, versions=4)
        projection = _bounded_graph_record_projection(
            sequence_locator=self.graph_sequence_locator(),
            intent_history=intent_history,
            intent_items=intent_items,
            plan_history=plan_history,
            plan_items=plan_items,
            draft_items=draft_items,
        )

        with patch(
            "tools.studyctl.graph_records.current_graph_record_locators",
            return_value=projection,
        ):
            selector_path = write_active_selector(paths)
        selector = load_json(selector_path)

        self.assertLess(selector_path.stat().st_size, 98_304)
        self.assertEqual(projection["sequence"], self.graph_sequence_locator())
        self.assertLessEqual(
            len(canonical_json_bytes(projection)),
            GRAPH_RECORD_LOCATOR_BUDGET_BYTES,
        )
        self.assertEqual(selector["graph_records"], projection)
        expected = (
            (
                "experiment_intents",
                intent_history,
                intent_items,
                "current_count",
            ),
            ("control_graphs", plan_history, plan_items, "current_count"),
            ("workspace_drafts", draft_items, draft_items, "total_count"),
        )
        for name, history, current, current_count_field in expected:
            with self.subTest(index=name):
                index = projection[name]
                self.assertEqual(index["total_count"], len(history))
                self.assertEqual(index[current_count_field], len(current))
                self.assertEqual(index["inventory_sha256"], sha256_json(history))
                self.assertGreater(index["selected_count"], 0)
                self.assertLess(index["selected_count"], len(current))
                self.assertTrue(index["truncated"])
                self.assertEqual(
                    index["items"],
                    current[: index["selected_count"]],
                )

    def test_graph_locator_projection_rejects_impossible_metadata_budget(
        self,
    ) -> None:
        (
            intent_history,
            intent_items,
            plan_history,
            plan_items,
            draft_items,
        ) = self.graph_locator_fixture(current_count=1, versions=1)

        with self.assertRaisesRegex(
            ValidationError,
            "metadata alone exceeds its canonical byte budget",
        ):
            _bounded_graph_record_projection(
                sequence_locator=self.graph_sequence_locator(),
                intent_history=intent_history,
                intent_items=intent_items,
                plan_history=plan_history,
                plan_items=plan_items,
                draft_items=draft_items,
                max_bytes=1,
            )

    def test_overlong_claim_id_is_rejected_and_cannot_expand_context(self) -> None:
        paths = self.initialize_approved_with_claim()
        claims = load_json(paths.claims)
        overlong = "CLAIM-" + "9" * 100_000
        claims["claims"][0]["claim_id"] = overlong
        claims["frontier"]["claim_ids"] = [overlong]
        atomic_write_json(paths.claims, claims)

        issues = object_schema_issues(paths.root, "claims", paths.claims, claims)
        self.assertTrue(
            any("string is longer than 64" in issue.message for issue in issues),
            [issue.render() for issue in issues],
        )
        with self.assertRaisesRegex(
            ValidationError,
            "active-context selector would exceed its structural byte budget",
        ):
            write_active_selector(paths)

    def test_canonical_byte_bound_is_exact_and_finalized_is_exempt(
        self,
    ) -> None:
        value = {"status": "draft", "payload": "abcd"}
        exact = len(canonical_json_bytes(value))
        schema = {
            "type": "object",
            "x-maxCanonicalBytes": exact,
            "x-maxCanonicalBytesStatuses": ["draft"],
        }
        self.assertEqual(validate_schema_instance(value, schema), [])

        oversized = {"status": "draft", "payload": "abcde"}
        errors = validate_schema_instance(oversized, schema)
        self.assertTrue(
            any(f"maximum is {exact}" in message for message in errors),
            errors,
        )
        finalized = {"status": "finalized", "payload": "x" * 100_000}
        self.assertEqual(validate_schema_instance(finalized, schema), [])

        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        draft = load_json(draft_path)
        draft["result"] = {"large_numeric_payload_belongs_in_objects": "x" * 70_000}
        issues = object_schema_issues(
            paths.root,
            "evidence",
            draft_path,
            draft,
        )
        self.assertTrue(
            any("maximum is 65536" in issue.message for issue in issues),
            [issue.render() for issue in issues],
        )

        draft["status"] = "finalized"
        finalized_messages = validate_schema_instance(
            draft,
            load_schema(paths.root, "evidence"),
        )
        self.assertFalse(
            any("canonical JSON" in message for message in finalized_messages),
            finalized_messages,
        )

    def test_review_and_status_use_bounded_other_evidence_source_indexes(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.initialize_git()
        self.commit_all("record bounded-projection fixture")
        marker = "EVIDENCE-PAYLOAD-MUST-NOT-BE-PROJECTED-" + "z" * 20_000
        finalized = self.finalized_other_evidence(paths, marker)

        packet_path = create_review_packet(paths)
        packet = load_json(packet_path)
        serialized_packet = packet_path.read_text(encoding="utf-8")
        self.assertNotIn(marker, serialized_packet)
        self.assertEqual(
            packet["other_evidence"][0]["evidence_id"],
            finalized["evidence_id"],
        )
        source = next(
            item for item in packet["evidence"] if item["role"] == "other"
        )
        self.assertEqual(source["assessment"], "inconclusive")
        self.assertEqual(source["summary"]["record_sha256"], finalized["record_sha256"])
        self.assertEqual(
            source["summary"]["inference"],
            {
                "observation_to_claim_present": True,
                "auxiliary_assumption_count": 1,
                "competing_explanation_count": 1,
                "falsification_condition_count": 1,
            },
        )
        self.assertNotIn("result", source["summary"])
        self.assertNotIn("scope", source["summary"])
        run_source = packet["other_run_sources"][0]
        self.assertNotIn("execution", run_source)
        self.assertNotIn("inputs", run_source)
        self.assertLess(packet_path.stat().st_size, 100_000)

        status_path = render_status(paths)
        status = status_path.read_text(encoding="utf-8")
        self.assertNotIn(marker, status)
        self.assertIn("## Other Active Evidence", status)
        self.assertIn("`EVID-0001` v1", status)
        self.assertIn(sha256_file(paths.evidence / "EVID-0001.v0001.json"), status)

    def test_soft_preflight_persists_advisory_without_dirtying_selector(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.set_run_pressure(soft=1, hard=2)
        selector_path = write_active_selector(paths)
        selector_hash = sha256_file(selector_path)

        pressure = require_growth_allowed(paths, "the next Run")
        self.assertEqual(pressure["level"], "normal")
        self.assertEqual(sha256_file(selector_path), selector_hash)

        advisory_path = runtime_compaction_due_path(paths)
        advisory = load_json(advisory_path)
        self.assertTrue(advisory["generated_projection"])
        self.assertEqual(advisory["current_level"], "normal")
        self.assertEqual(advisory["projected_level"], "soft")
        self.assertTrue(advisory["compaction_due"])
        self.assertFalse(advisory["growth_blocked_now"])
        self.assertIn("runs_since_checkpoint=1", " ".join(advisory["reasons"]))


if __name__ == "__main__":
    unittest.main()
