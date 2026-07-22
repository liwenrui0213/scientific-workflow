from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import unittest

from tests.helpers import REPOSITORY_ROOT, WorkflowTestCase
from tools.studyctl.hashing import atomic_write_json, load_json


HOOK_PATH = REPOSITORY_ROOT / ".codex" / "hooks" / "pre_tool_policy.py"
HOOK_SPEC = importlib.util.spec_from_file_location("pre_tool_policy_under_test", HOOK_PATH)
if HOOK_SPEC is None or HOOK_SPEC.loader is None:
    raise RuntimeError(f"could not load hook policy from {HOOK_PATH}")
HOOK_POLICY = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(HOOK_POLICY)


class HookPolicyTests(WorkflowTestCase):
    def bash_event(self, command: str) -> dict[str, Any]:
        return {
            "tool_name": "Bash",
            "cwd": str(self.root),
            "tool_input": {"command": command},
        }

    def patch_event(self, action: str, path: str) -> dict[str, Any]:
        return {
            "tool_name": "ApplyPatch",
            "cwd": str(self.root),
            "tool_input": {
                "patch": (
                    "*** Begin Patch\n"
                    f"*** {action} File: {path}\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch\n"
                )
            },
        }

    def direct_file_event(self, tool_name: str, path: str) -> dict[str, Any]:
        return {
            "tool_name": tool_name,
            "cwd": str(self.root),
            "tool_input": {"file_path": path, "content": "replacement\n"},
        }

    def test_human_only_approval_and_verdict_commands_are_blocked(self) -> None:
        commands = (
            "python -m tools.studyctl approve-brief SC-0001",
            "python -m tools.studyctl verdict SC-0001 --file proposed-verdict.json",
        )
        for command in commands:
            with self.subTest(command=command):
                reason = HOOK_POLICY.decide(self.bash_event(command))
                self.assertEqual(
                    reason,
                    "Codex must not invoke the human-only approve-brief or verdict command.",
                )

    def test_checkpoint_and_sequence_authority_are_blocked_from_direct_edits(self) -> None:
        paths = self.initialize()
        expected = (
            "Checkpoint, sequence, and archived Claim records are sealed "
            "authority and must not be changed or removed directly."
        )
        for relative in (
            f"studies/{paths.study_id}/CHECKPOINTS.sequence.json",
            f"studies/{paths.study_id}/EVIDENCE.sequence.json",
            f"studies/{paths.study_id}/checkpoints/CHECKPOINT-000001.json",
            f"studies/{paths.study_id}/checkpoints/claim-records/CLAIM-0001.{'0' * 64}.json",
        ):
            with self.subTest(relative=relative):
                self.assertEqual(
                    HOOK_POLICY.decide(self.patch_event("Update", relative)),
                    expected,
                )

    def test_human_owned_and_sealed_records_are_blocked(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        claims = load_json(paths.claims)
        claims["claims"][0]["other_evidence"] = [
            {
                "evidence_id": "EVID-0009",
                "version": 1,
                "sha256": "0" * 64,
            }
        ]
        atomic_write_json(paths.claims, claims)
        referenced_evidence = paths.evidence / "EVID-0009.v0001.json"
        atomic_write_json(referenced_evidence, {"status": "finalized"})

        cases = (
            (
                self.bash_event(f"rm studies/{paths.study_id}/BRIEF.approval.json"),
                "Brief approval records may be written only by the interactive studyctl gate.",
            ),
            (
                self.patch_event("Add", f"studies/{paths.study_id}/VERDICT.json"),
                "Verdict records may be written only by the interactive studyctl gate.",
            ),
            (
                self.bash_event(
                    f"sed -i '' 's/four/five/' studies/{paths.study_id}/BRIEF.md"
                ),
                "An approved Brief must be revised through studyctl brief-new-version.",
            ),
            (
                self.patch_event(
                    "Update",
                    f"studies/{paths.study_id}/runs/{manifest['run_id']}/manifest.json",
                ),
                "Run manifests are sealed execution records and must not be changed or removed.",
            ),
            (
                self.patch_event(
                    "Add",
                    f"studies/{paths.study_id}/runs/RUN-999999/manifest.json",
                ),
                "Run manifests are sealed execution records and must not be changed or removed.",
            ),
            (
                self.bash_event(
                    f"rm -rf studies/{paths.study_id}/runs/{manifest['run_id']}"
                ),
                "Run manifests are sealed execution records and must not be changed or removed.",
            ),
            (
                self.patch_event(
                    "Update",
                    f"studies/{paths.study_id}/RUNS.ledger.json",
                ),
                "Run manifests are sealed execution records and must not be changed or removed.",
            ),
            (
                self.bash_event(
                    f"tee studies/{paths.study_id}/evidence/EVID-0009.v0001.json"
                ),
                "Evidence referenced by a Claim is immutable; create a new Evidence version.",
            ),
            (
                self.bash_event(f"rm -rf studies/{paths.study_id}/evidence"),
                "Evidence referenced by a Claim is immutable; create a new Evidence version.",
            ),
            (
                self.direct_file_event(
                    "Write",
                    f"studies/{paths.study_id}/BRIEF.approval.json",
                ),
                "Brief approval records may be written only by the interactive studyctl gate.",
            ),
            (
                self.patch_event(
                    "Add",
                    f"studies/{paths.study_id}/formal/CHANGESET.json",
                ),
                "CHANGESET records may be written only by studyctl changeset-new.",
            ),
            (
                self.bash_event(
                    f"tee studies/{paths.study_id}/formal/VALIDATION.json"
                ),
                "Validation proofs may be written only by studyctl validate-changes.",
            ),
            (
                self.bash_event(
                    "python -c \"open('studies/SC-0001/formal/CHANGESET.json', 'w').write('{}')\""
                ),
                "CHANGESET records may be written only by studyctl changeset-new.",
            ),
            (
                self.bash_event(
                    "python -c \"from pathlib import Path; "
                    "Path('studies/SC-0001/formal/VALIDATION.json').open('r+').write('{}')\""
                ),
                "Validation proofs may be written only by studyctl validate-changes.",
            ),
            (
                self.patch_event(
                    "Add",
                    f"studies/{paths.study_id}/formal/confirmations/CONF-0001.json",
                ),
                "Frozen Confirmation Records may be written only by studyctl confirmation-finalize.",
            ),
            (
                self.bash_event(
                    f"rm studies/{paths.study_id}/formal/confirmations/CONF-0001.json"
                ),
                "Frozen Confirmation Records may be written only by studyctl confirmation-finalize.",
            ),
            (
                self.direct_file_event(
                    "Edit",
                    f"studies/{paths.study_id}/evidence/EVID-0009.v0001.json".lower(),
                ),
                "Evidence referenced by a Claim is immutable; create a new Evidence version.",
            ),
        )
        for event, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                self.assertEqual(HOOK_POLICY.decide(event), expected_reason)

    def test_benign_reads_unapproved_brief_and_unreferenced_draft_are_permitted(self) -> None:
        paths = self.initialize()
        draft = paths.evidence / "EVID-0002.v0001.json"
        atomic_write_json(draft, {"status": "draft"})

        permitted = (
            self.bash_event(f"sed -n '1,20p' studies/{paths.study_id}/BRIEF.md"),
            self.patch_event("Update", f"studies/{paths.study_id}/BRIEF.md"),
            self.patch_event(
                "Update",
                f"studies/{paths.study_id}/evidence/EVID-0002.v0001.json",
            ),
        )
        for event in permitted:
            with self.subTest(event=event):
                self.assertIsNone(HOOK_POLICY.decide(event))

    def test_hook_uses_profile_configured_study_root(self) -> None:
        profile_path = self.root / "scientific-workflow" / "repository-profile.json"
        profile = load_json(profile_path)
        profile["study_root"] = "research/studies"
        profile["generated_patterns"] = [
            "**/__pycache__/**",
            "research/studies/*/generated/**",
        ]
        atomic_write_json(profile_path, profile)
        paths = self.initialize_approved_with_claim()

        custom_path = paths.brief.relative_to(self.root.resolve()).as_posix()
        reason = HOOK_POLICY.decide(self.patch_event("Update", custom_path))

        self.assertEqual(
            reason,
            "An approved Brief must be revised through studyctl brief-new-version.",
        )
        self.assertIsNone(
            HOOK_POLICY.decide(
                self.patch_event("Update", f"studies/{paths.study_id}/BRIEF.md")
            )
        )

    def test_malformed_hook_input_fails_closed(self) -> None:
        for malformed in ("{", "[]"):
            with self.subTest(input=malformed):
                result = subprocess.run(
                    [sys.executable, str(HOOK_PATH)],
                    cwd=self.root,
                    input=malformed,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    shell=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                denial = json.loads(result.stdout)
                hook_output = denial["hookSpecificOutput"]
                self.assertEqual(hook_output["hookEventName"], "PreToolUse")
                self.assertEqual(hook_output["permissionDecision"], "deny")
                self.assertIn(
                    "Scientific workflow hook could not safely inspect this tool call",
                    hook_output["permissionDecisionReason"],
                )
                self.assertEqual(denial["systemMessage"], hook_output["permissionDecisionReason"])

    def test_malformed_claims_fail_closed_for_evidence_mutation(self) -> None:
        paths = self.initialize()
        draft = paths.evidence / "EVID-0002.v0001.json"
        atomic_write_json(draft, {"status": "draft"})
        paths.claims.write_text("{", encoding="utf-8")

        reason = HOOK_POLICY.decide(
            self.direct_file_event(
                "Edit",
                f"studies/{paths.study_id}/evidence/{draft.name}",
            )
        )

        self.assertEqual(
            reason,
            "Cannot safely verify Claim references; repair CLAIMS.json before changing Evidence.",
        )


if __name__ == "__main__":
    unittest.main()
