from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from .checkpoint_sequence import empty_checkpoint_sequence, write_checkpoint_sequence
from .evidence_sequence import empty_evidence_sequence, write_evidence_sequence
from .graph_record_sequence import (
    empty_graph_record_sequence,
    write_graph_record_sequence,
)
from .execution_backends import SUPPORTED_EXECUTION_BACKENDS
from .observation_sequence import (
    empty_observation_sequence,
    write_observation_sequence,
)
from .hashing import atomic_write_bytes, atomic_write_json
from .models import (
    HumanGateError,
    RunInterrupted,
    ValidationError,
    WorkflowError,
    get_repo_root,
    require_id,
    study_paths,
    utc_now,
)
from .run_ledger import empty_ledger, write_ledger
from .validation import validate_study


def initialize_study(root: Path, study_id: str, title: str) -> Path:
    require_id("study", study_id)
    if not title.strip():
        raise ValidationError("title must not be empty")
    from .observation_triggers import load_current_registry
    from .workspace import load_repository_profile

    load_repository_profile(root)
    load_current_registry(root)
    paths = study_paths(root, study_id, must_exist=False)
    if paths.study.exists():
        raise WorkflowError(f"refusing to overwrite existing study: {study_id}")
    directories = (
        paths.formal,
        paths.confirmations,
        paths.control_graphs,
        paths.experiment_intents,
        paths.active_work,
        paths.archived_work,
        paths.runs,
        paths.observations,
        paths.evidence,
        paths.failed_directions,
        paths.checkpoints,
        paths.generated,
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=False if directory == paths.formal else True)
    write_ledger(paths, empty_ledger(paths))
    write_observation_sequence(
        paths, empty_observation_sequence(paths), overwrite=False
    )
    write_evidence_sequence(paths, empty_evidence_sequence(paths), overwrite=False)
    write_checkpoint_sequence(
        paths, empty_checkpoint_sequence(paths), overwrite=False
    )
    write_graph_record_sequence(
        paths, empty_graph_record_sequence(paths), overwrite=False
    )
    template_root = root / "scientific-workflow" / "templates"
    brief_template = (template_root / "BRIEF.md").read_text(encoding="utf-8")
    brief_text = brief_template.replace("{{STUDY_ID}}", study_id).replace("{{TITLE}}", title.strip())
    claims_template = (template_root / "CLAIMS.json").read_text(encoding="utf-8")
    claims_text = claims_template.replace("{{STUDY_ID}}", study_id).replace("{{TIMESTAMP}}", utc_now())
    atomic_write_bytes(paths.brief, brief_text.encode("utf-8"), overwrite=False)
    try:
        claims = json.loads(claims_text)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"invalid repository CLAIMS template: {exc}") from exc
    atomic_write_json(paths.claims, claims, overwrite=False)
    from .rendering import render_status

    render_status(paths)
    paths.assert_safe_layout(must_exist=True)
    return paths.study


def _root_from_args(raw: str | None) -> Path:
    return Path(raw).resolve() if raw else get_repo_root()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def _parse_seed(value: str) -> int | str | None:
    """Preserve a frozen seed's JSON scalar type at the CLI boundary.

    Plain non-JSON text remains a string, while ``17`` becomes the integer 17,
    ``null`` becomes ``None``, and a quoted JSON string can deliberately retain
    numeric-looking text such as ``"17"``.
    """

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, bool) or not isinstance(parsed, (int, str, type(None))):
        raise argparse.ArgumentTypeError(
            "seed must be a JSON integer, string, null, or plain string"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="studyctl", description="Claim-to-Evidence Scientific Workflow"
    )
    parser.add_argument("--root", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    subparsers.add_parser(
        "profile-validate", help="validate the repository adaptation profile"
    )

    resolve = subparsers.add_parser(
        "resolve-study",
        help="resolve a continuation without creating a Study",
    )
    resolve.add_argument("study_id", nargs="?")

    init = subparsers.add_parser("init", help="initialize a Study")
    init.add_argument("study_id")
    init.add_argument("--title", required=True)

    approve = subparsers.add_parser("approve-brief", help="human-approve the active Brief")
    approve.add_argument("study_id")

    new_brief = subparsers.add_parser("brief-new-version", help="preserve an approved Brief and open a new draft")
    new_brief.add_argument("study_id")

    ledger_migrate = subparsers.add_parser(
        "ledger-migrate",
        help="explicitly index an intact contiguous pre-V3 Run history",
    )
    ledger_migrate.add_argument("study_id")

    graph_sequence_recover = subparsers.add_parser(
        "recover-graph-record-sequence",
        help=(
            "advance across exactly one valid graph record left unindexed by "
            "an interrupted finalization"
        ),
    )
    graph_sequence_recover.add_argument("study_id")

    validate = subparsers.add_parser("validate", help="validate authoritative records and references")
    validate.add_argument("study_id")

    status = subparsers.add_parser("status", help="regenerate deterministic STATUS.md")
    status.add_argument("study_id")

    context = subparsers.add_parser(
        "context",
        help="regenerate the bounded ACTIVE_CONTEXT.json selector",
    )
    context.add_argument("study_id")

    formalization = subparsers.add_parser("check-formalization", help="evaluate progressive-formalization gates")
    formalization.add_argument("study_id")
    formalization.add_argument("--estimated-gpu-hours", type=float, default=0.0)
    formalization.add_argument("--estimated-cpu-hours", type=float, default=0.0)
    formalization.add_argument("--estimated-storage-gb", type=float, default=0.0)
    formalization.add_argument("--changed-path", action="append", default=[])
    formalization.add_argument("--changes-evaluator", action="store_true")
    formalization.add_argument("--changes-dataset-split", action="store_true")
    formalization.add_argument("--changes-acceptance-criteria", action="store_true")
    formalization.add_argument("--changes-claim-scope", action="store_true")
    formalization.add_argument("--parallel-workers", type=int, default=1)
    formalization.add_argument("--scientific-critical", action="store_true")
    formalization.add_argument("--shared-across-runs", action="store_true")
    formalization.add_argument("--has-parallel-dependencies", action="store_true")
    formalization.add_argument("--for-evidence", action="store_true")
    formalization.add_argument("--for-review", action="store_true")

    changeset_new = subparsers.add_parser(
        "changeset-new", help="create the Study source/test write contract"
    )
    changeset_new.add_argument("study_id")
    changeset_new.add_argument("--allow", action="append", required=True)
    changeset_new.add_argument("--base-ref")

    changeset_renew = subparsers.add_parser(
        "changeset-renew",
        help="archive and replace a stale Study change contract after an explicit rebase/sync",
    )
    changeset_renew.add_argument("study_id")
    changeset_renew.add_argument("--allow", action="append")
    changeset_renew.add_argument("--base-ref")

    check_changes = subparsers.add_parser(
        "check-changes", help="verify actual Git changes against the Study contract"
    )
    check_changes.add_argument("study_id")

    validate_changes = subparsers.add_parser(
        "validate-changes",
        help="run repository-native validation and seal a commit-pinned proof",
    )
    validate_changes.add_argument("study_id")

    confirmation_new = subparsers.add_parser(
        "confirmation-new",
        help="create a small pre-confirmatory-Run Confirmation Record draft",
    )
    confirmation_new.add_argument("study_id")
    confirmation_new.add_argument("--id", dest="confirmation_id", required=True)
    confirmation_new.add_argument("--claim", action="append", required=True)

    confirmation_finalize = subparsers.add_parser(
        "confirmation-finalize",
        help="freeze a Confirmation Record before confirmatory Runs",
    )
    confirmation_finalize.add_argument("study_id")
    confirmation_finalize.add_argument("--file", required=True)

    intent_new = subparsers.add_parser(
        "intent-new",
        help="create a versioned ExperimentIntent draft (why evidence is needed)",
    )
    intent_new.add_argument("study_id")
    intent_new.add_argument("--id", dest="intent_id", required=True)
    intent_new.add_argument("--evidence-gap-id", required=True)
    intent_new.add_argument("--evidence-gap", required=True)
    intent_new.add_argument("--objective", required=True)
    intent_new.add_argument(
        "--requested-observation", action="append", required=True
    )
    intent_new.add_argument("--evidence-requirement", action="append", default=[])
    intent_new.add_argument("--claim")

    intent_finalize = subparsers.add_parser(
        "intent-finalize",
        help="freeze an ExperimentIntent after its evidence semantics are complete",
    )
    intent_finalize.add_argument("study_id")
    intent_finalize.add_argument("--file", required=True)

    plan_new = subparsers.add_parser(
        "plan-new",
        help="create a ControlGraphSpec draft (how an exact Intent will execute)",
    )
    plan_new.add_argument("study_id")
    plan_new.add_argument("--id", dest="control_graph_id", required=True)
    plan_new.add_argument("--intent", dest="intent_id", required=True)
    plan_new.add_argument("--intent-version", type=int, required=True)
    plan_new.add_argument("--executor", default="studyctl")
    plan_new.add_argument("--cpu-hours", type=float, default=0.0)
    plan_new.add_argument("--gpu-hours", type=float, default=0.0)
    plan_new.add_argument("--storage-gb", type=float, default=0.0)
    plan_new.add_argument("--parallel-workers", type=int, default=1)

    plan_finalize = subparsers.add_parser(
        "plan-finalize",
        help="freeze a ControlGraphSpec bound to an exact ExperimentIntent",
    )
    plan_finalize.add_argument("study_id")
    plan_finalize.add_argument("--file", required=True)

    plan_activate = subparsers.add_parser(
        "plan-activate",
        help="materialize a current ControlGraphSpec as formal/PLAN.json",
    )
    plan_activate.add_argument("study_id")
    plan_activate.add_argument("--id", dest="control_graph_id", required=True)
    plan_activate.add_argument("--version", type=int, required=True)

    run = subparsers.add_parser("run", help="execute and seal a reproducible Run")
    run.add_argument("study_id")
    run.add_argument("--purpose", required=True)
    run.add_argument(
        "--mode",
        choices=("exploratory", "confirmatory"),
        default="exploratory",
        help="epistemic role; exploratory is the default",
    )
    run.add_argument("--confirmation")
    run.add_argument("--slot")
    run.add_argument("--cohort")
    run.add_argument("--estimated-gpu-hours", type=float, default=0.0)
    run.add_argument("--estimated-cpu-hours", type=float, default=0.0)
    run.add_argument("--estimated-storage-gb", type=float, default=0.0)
    run.add_argument("--input", action="append", default=[])
    run.add_argument("--output", action="append", default=[])
    run.add_argument("--pin-output", action="append", default=[])
    run.add_argument("--baseline-output", action="append", default=[])
    run.add_argument("--unique-anomaly-output", action="append", default=[])
    run.add_argument("--changed-path", action="append", default=[])
    run.add_argument("--scientific-critical", action="store_true")
    run.add_argument("--shared-across-runs", action="store_true")
    run.add_argument("--seed", type=_parse_seed)
    run.add_argument("--hardware-class")
    run.add_argument("--precision")
    run.add_argument("--cohort-field", action="append", default=[])
    run.add_argument(
        "--execution-backend",
        choices=("auto", *SUPPORTED_EXECUTION_BACKENDS),
        default="auto",
        help="sealed execution backend; auto follows repository-profile preference",
    )
    # ``+`` lets argparse continue recognizing Run options after STUDY_ID;
    # the explicit ``--`` then terminates studyctl parsing and preserves every
    # remaining command argument literally.
    run.add_argument("command", nargs="+")

    observation_new = subparsers.add_parser(
        "observation-new",
        help="promote Run analysis into an optional draft Observation Record",
    )
    observation_new.add_argument("study_id")
    observation_new.add_argument("--id", dest="observation_id", required=True)
    observation_new.add_argument(
        "--run", dest="run_ids", action="append", required=True
    )
    observation_new.add_argument(
        "--trigger", dest="promotion_triggers", action="append", required=True
    )

    observation_finalize = subparsers.add_parser(
        "observation-finalize",
        help="seal a completed Observation draft",
    )
    observation_finalize.add_argument("study_id")
    observation_finalize.add_argument("--file", required=True)

    observation_triggers = subparsers.add_parser(
        "observation-trigger-list",
        help="list the active versioned Observation promotion-trigger registry",
    )
    observation_triggers.add_argument("study_id")

    evidence_new = subparsers.add_parser("evidence-new", help="create a schema-valid draft Evidence version")
    evidence_new.add_argument("study_id")
    evidence_new.add_argument("--id", dest="evidence_id", required=True)
    evidence_new.add_argument("--claim", action="append", required=True)
    evidence_new.add_argument("--run", dest="run_ids", action="append", required=True)
    evidence_new.add_argument("--observation-id")
    evidence_new.add_argument("--observation-version", type=int)

    evidence_finalize = subparsers.add_parser("evidence-finalize", help="seal a completed Evidence draft")
    evidence_finalize.add_argument("study_id")
    evidence_finalize.add_argument("--file", required=True)

    compact_prepare = subparsers.add_parser("compact-prepare", help="build deterministic compaction input")
    compact_prepare.add_argument("study_id")

    compact_finalize = subparsers.add_parser("compact-finalize", help="validate and apply a compaction plan")
    compact_finalize.add_argument("study_id")
    compact_finalize.add_argument("--plan", required=True)

    review_packet = subparsers.add_parser("review-packet", help="build an independent-review packet")
    review_packet.add_argument("study_id")
    review_packet.add_argument("--base-ref")

    review_render = subparsers.add_parser("review-render", help="validate structured review JSON and render REVIEW.md")
    review_render.add_argument("study_id")
    review_render.add_argument("--file", required=True)

    verdict = subparsers.add_parser(
        "verdict",
        help="record human-owned decisions in an auto-scoped Verdict",
    )
    verdict.add_argument("study_id")
    verdict.add_argument(
        "--file",
        help="optional prewritten human decision input; scope and hashes are generated",
    )
    verdict.add_argument(
        "--agent-initiated",
        action="store_true",
        help=(
            "record a version-2 decision input authorized by an explicit user instruction; "
            "requires --file and does not require a terminal confirmation"
        ),
    )

    gc = subparsers.add_parser(
        "gc", help="report safe object-deletion candidates; this command is always dry-run"
    )
    gc.add_argument("study_id")
    gc.add_argument("--dry-run", action="store_true")
    return parser


def dispatch(args: argparse.Namespace) -> int:
    root = _root_from_args(args.root)
    name = args.command_name
    if name == "profile-validate":
        from .workspace import profile_summary

        summary = profile_summary(root)
        _print_json({"validation": "PASS", **summary})
        return 0
    if name == "resolve-study":
        from .study_routing import resolve_study

        _print_json(resolve_study(root, args.study_id).route_record())
        return 0
    if name == "init":
        path = initialize_study(root, args.study_id, args.title)
        print(path)
        return 0
    paths = study_paths(root, args.study_id)
    if name == "validate":
        issues = validate_study(paths)
        for issue in issues:
            print(issue.render())
        errors = [issue for issue in issues if issue.level == "ERROR"]
        from .workspace import evaluate_changes

        change_state = evaluate_changes(paths)
        for violation in change_state.get("violations", []):
            target = violation.get("path") or "<repository>"
            print(
                f"ERROR: {target}: change scope {violation.get('rule')}: "
                f"{violation.get('reason')}"
            )
        total_errors = len(errors) + len(change_state.get("violations", []))
        print(
            f"validation: {'FAILED' if total_errors else 'PASS'} "
            f"({total_errors} error(s), {len(issues) - len(errors)} warning(s))"
        )
        return 1 if total_errors else 0
    if name == "approve-brief":
        from .approval import approve_brief

        approve_brief(paths)
        return 0
    if name == "brief-new-version":
        from .approval import begin_brief_revision

        print(begin_brief_revision(paths))
        return 0
    if name == "ledger-migrate":
        from .run_registry import migrate_legacy_run_ledger

        print(migrate_legacy_run_ledger(paths))
        return 0
    if name == "recover-graph-record-sequence":
        from .graph_records import recover_graph_record_sequence

        print(recover_graph_record_sequence(paths))
        return 0
    if name == "status":
        from .rendering import render_status

        print(render_status(paths))
        return 0
    if name == "context":
        from .active_context import refresh_active_projection

        selector, _ = refresh_active_projection(paths)
        print(selector)
        return 0
    if name == "check-formalization":
        from .formalization import check_formalization

        options = vars(args).copy()
        options["check_hard_budget"] = True
        result = check_formalization(paths, options)
        print(result.outcome)
        for requirement in result.requirements:
            print(f"- {requirement['level']}: {requirement['artifact']}: {requirement['reason']}")
        return 2 if result.blocked else 0
    if name == "changeset-new":
        from .workspace import create_changeset

        print(
            create_changeset(
                paths,
                args.allow,
                base_ref=args.base_ref,
            )
        )
        return 0
    if name == "changeset-renew":
        from .workspace import renew_changeset

        print(
            renew_changeset(
                paths,
                args.allow,
                base_ref=args.base_ref,
            )
        )
        return 0
    if name == "check-changes":
        from .workspace import evaluate_changes

        result = evaluate_changes(paths, write_projection=True)
        print(result["outcome"])
        for record in result["changed_paths"]:
            tracked = "tracked" if record["tracked"] else "untracked"
            print(f"- {record['classification']}: {tracked}: {record['path']}")
        for violation in result["violations"]:
            target = violation["path"] or "<repository>"
            print(f"- BLOCKED: {violation['rule']}: {target}: {violation['reason']}")
        for advisory in result["advisories"]:
            print(f"- ADVISORY: {advisory}")
        return 2 if result["outcome"] == "BLOCKED" else 0
    if name == "validate-changes":
        from .workspace import run_change_validation

        proof = run_change_validation(paths)
        _print_json(
            {
                "path": "formal/VALIDATION.json",
                "passed": proof["passed"],
                "commands": [
                    {
                        "name": item["name"],
                        "exit_code": item["exit_code"],
                    }
                    for item in proof["commands"]
                ],
            }
        )
        return 0 if proof["passed"] else 2
    if name == "run":
        from .run_registry import execute_run

        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        manifest = execute_run(
            paths,
            argv=command,
            purpose=args.purpose,
            epistemic_mode=args.mode,
            confirmation_id=args.confirmation,
            confirmation_slot=args.slot,
            cohort_id=args.cohort,
            estimated_gpu_hours=args.estimated_gpu_hours,
            estimated_cpu_hours=args.estimated_cpu_hours,
            estimated_storage_gb=args.estimated_storage_gb,
            input_paths=args.input,
            output_paths=args.output,
            pinned_outputs=args.pin_output,
            baseline_outputs=args.baseline_output,
            unique_anomaly_outputs=args.unique_anomaly_output,
            changed_paths=args.changed_path,
            scientific_critical=args.scientific_critical,
            shared_across_runs=args.shared_across_runs,
            seed=args.seed,
            hardware_class=args.hardware_class,
            precision=args.precision,
            cohort_fields=args.cohort_field,
            execution_backend=args.execution_backend,
        )
        _print_json({"run_id": manifest["run_id"], "status": manifest["status"], "exit_code": manifest["execution"]["exit_code"]})
        status = manifest["status"]
        raw_exit_code = manifest["execution"]["exit_code"]
        if status == "succeeded" and manifest.get("change_scope", {}).get(
            "evidence_eligible", False
        ):
            return 0
        if status == "failed" and isinstance(raw_exit_code, int) and raw_exit_code != 0:
            # subprocess uses negative return codes for signal termination,
            # while a CLI process must return an unsigned shell status.
            return 128 + abs(raw_exit_code) if raw_exit_code < 0 else raw_exit_code
        if status == "interrupted":
            return 130
        if status in {"incomplete", "running"}:
            return 2
        return 2
    if name == "confirmation-new":
        from .confirmation import create_confirmation_draft

        print(
            create_confirmation_draft(
                paths,
                args.confirmation_id,
                args.claim,
            )
        )
        return 0
    if name == "confirmation-finalize":
        from .confirmation import finalize_confirmation

        print(finalize_confirmation(paths, Path(args.file)))
        return 0
    if name == "intent-new":
        from .graph_records import create_experiment_intent_draft

        print(
            create_experiment_intent_draft(
                paths,
                args.intent_id,
                evidence_gap_id=args.evidence_gap_id,
                evidence_gap=args.evidence_gap,
                objective=args.objective,
                requested_observations=args.requested_observation,
                evidence_requirements=args.evidence_requirement,
                claim_id=args.claim,
            )
        )
        return 0
    if name == "intent-finalize":
        from .graph_records import finalize_experiment_intent

        print(finalize_experiment_intent(paths, Path(args.file)))
        return 0
    if name == "plan-new":
        from .graph_records import create_control_graph_draft

        print(
            create_control_graph_draft(
                paths,
                args.control_graph_id,
                intent_id=args.intent_id,
                intent_version=args.intent_version,
                executor=args.executor,
                cpu_hours=args.cpu_hours,
                gpu_hours=args.gpu_hours,
                storage_gb=args.storage_gb,
                parallel_workers=args.parallel_workers,
            )
        )
        return 0
    if name == "plan-finalize":
        from .graph_records import finalize_control_graph

        print(finalize_control_graph(paths, Path(args.file)))
        return 0
    if name == "plan-activate":
        from .graph_records import activate_control_graph

        print(
            activate_control_graph(
                paths,
                args.control_graph_id,
                args.version,
            )
        )
        return 0
    if name == "observation-new":
        from .observation import create_observation_draft

        print(
            create_observation_draft(
                paths,
                args.observation_id,
                args.run_ids,
                args.promotion_triggers,
            )
        )
        return 0
    if name == "observation-finalize":
        from .observation import finalize_observation

        print(finalize_observation(paths, Path(args.file)))
        return 0
    if name == "observation-trigger-list":
        from .observation_triggers import load_current_registry

        registry = load_current_registry(paths.root)
        _print_json(
            {
                "registry_version": registry["registry_version"],
                "registry_sha256": registry["registry_sha256"],
                "triggers": [
                    {
                        "id": item["id"],
                        "kind": item["kind"],
                        "confirmatory_allowed": item[
                            "confirmatory_allowed"
                        ],
                        "description": item["description"],
                        "origin": item["governance"]["origin"],
                    }
                    for item in registry["triggers"]
                ],
            }
        )
        return 0
    if name == "evidence-new":
        from .evidence import create_evidence_draft

        print(
            create_evidence_draft(
                paths,
                args.evidence_id,
                args.claim,
                args.run_ids,
                observation_id=args.observation_id,
                observation_version=args.observation_version,
            )
        )
        return 0
    if name == "evidence-finalize":
        from .evidence import finalize_evidence

        print(finalize_evidence(paths, Path(args.file)))
        return 0
    if name == "compact-prepare":
        from .compaction import prepare_compaction

        print(prepare_compaction(paths))
        return 0
    if name == "compact-finalize":
        from .compaction import finalize_compaction

        print(finalize_compaction(paths, Path(args.plan)))
        return 0
    if name == "review-packet":
        from .review import create_review_packet

        print(create_review_packet(paths, args.base_ref))
        return 0
    if name == "review-render":
        from .review import import_and_render_review

        print(import_and_render_review(paths, Path(args.file)))
        return 0
    if name == "verdict":
        from .approval import record_verdict

        source = Path(args.file) if args.file else None
        print(record_verdict(paths, source, agent_initiated=args.agent_initiated))
        return 0
    if name == "gc":
        if not args.dry_run:
            raise WorkflowError("garbage collection is dry-run only; pass --dry-run")
        from .gc import garbage_collection_report

        _print_json(garbage_collection_report(paths))
        return 0
    raise WorkflowError(f"unimplemented command: {name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return dispatch(args)
    except RunInterrupted as exc:
        print(f"interrupted: {exc}", file=sys.stderr)
        return 130
    except (WorkflowError, ValidationError, HumanGateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
