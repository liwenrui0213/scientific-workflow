from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .hashing import sha256_json
from .models import StudyPaths, ValidationError, require_id


def validate_run_intent_binding(
    paths: StudyPaths,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Replay one optional Run-to-ExperimentIntent binding.

    This binding is independent of PLAN: a Run can durably state why it was
    performed without introducing a ControlGraphSpec.  When a PLAN binding is
    also present, ``validate_run_control_binding`` requires the two references
    to be identical.
    """

    binding = manifest.get("intent_binding")
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise ValidationError("Run intent_binding must be an object or null")

    intent_id = require_id(
        "experiment_intent", str(binding.get("intent_id", ""))
    )
    version = binding.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("Run Intent version must be a positive integer")

    from .graph_records import load_final_experiment_intent

    intent = load_final_experiment_intent(paths, intent_id, version)
    if binding.get("sha256") != intent.get("record_sha256"):
        raise ValidationError(
            "Run intent_binding digest does not match the finalized "
            "Experiment Intent"
        )
    return binding


def validate_run_control_binding(
    paths: StudyPaths,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Replay one optional Run-to-ControlGraph binding from immutable sources."""

    intent_binding = validate_run_intent_binding(paths, manifest)
    binding = manifest.get("control_binding")
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise ValidationError("Run control_binding must be an object or null")

    from .graph_records import load_final_control_graph

    graph = load_final_control_graph(
        paths,
        str(binding.get("control_graph_id", "")),
        binding.get("version"),
    )
    if binding.get("sha256") != graph.get("record_sha256"):
        raise ValidationError(
            "Run control_binding graph digest does not match the finalized "
            "Control Graph"
        )
    if binding.get("intent_ref") != graph.get("realizes_intent"):
        raise ValidationError(
            "Run control_binding Intent reference does not match the finalized "
            "Control Graph"
        )
    if intent_binding is None:
        raise ValidationError(
            "Run control_binding requires an independent intent_binding"
        )
    if binding.get("intent_ref") != intent_binding:
        raise ValidationError(
            "Run control_binding Intent reference does not match its "
            "independent intent_binding"
        )
    nodes = graph.get("nodes")
    node = next(
        (
            candidate
            for candidate in nodes
            if isinstance(candidate, dict)
            and candidate.get("node_id") == binding.get("node_id")
        ),
        None,
    ) if isinstance(nodes, list) else None
    if node is None:
        raise ValidationError(
            "Run control_binding node does not exist in the finalized "
            "Control Graph"
        )
    if binding.get("node_spec_sha256") != sha256_json(node):
        raise ValidationError(
            "Run control_binding node digest does not match the finalized "
            "Control Graph node"
        )
    command = node.get("command")
    execution = manifest.get("execution")
    argv = execution.get("argv") if isinstance(execution, dict) else None
    if command is not None and command != argv:
        raise ValidationError(
            "Run execution argv does not match its bound Control Graph node "
            "command"
        )
    return binding


def intent_refs_from_manifests(
    paths: StudyPaths,
    manifests: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive the exact cognitive Intents realized by source Runs.

    Runs may bind an Intent without a PLAN.  A null or absent
    ``intent_binding`` contributes no Intent reference, which keeps informal
    exploration legal while preserving a durable why whenever one is declared.
    """

    refs: dict[tuple[str, int], dict[str, Any]] = {}
    for manifest in manifests:
        validate_run_control_binding(paths, manifest)
        binding = validate_run_intent_binding(paths, manifest)
        if binding is None:
            continue
        raw = binding
        if not isinstance(raw, dict):
            raise ValidationError("bound Run requires an exact intent_binding")
        intent_id = require_id(
            "experiment_intent", str(raw.get("intent_id", ""))
        )
        version = raw.get("version")
        digest = raw.get("sha256")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            raise ValidationError("bound Run Intent version must be positive")
        if not isinstance(digest, str):
            raise ValidationError("bound Run Intent sha256 must be a string")

        key = (intent_id, version)
        candidate = {
            "intent_id": intent_id,
            "version": version,
            "sha256": digest,
        }
        previous = refs.setdefault(key, candidate)
        if previous != candidate:
            raise ValidationError(
                f"source Runs disagree about Intent identity: {intent_id} v{version}"
            )
    return [refs[key] for key in sorted(refs)]


def validate_exact_intent_refs(
    paths: StudyPaths,
    declared: Any,
    manifests: Sequence[dict[str, Any]],
    *,
    record_label: str,
) -> list[dict[str, Any]]:
    """Require a record's Intent references to equal its Run-derived set."""

    if not isinstance(declared, list):
        raise ValidationError(f"{record_label} intent_refs must be an array")
    expected = intent_refs_from_manifests(paths, manifests)
    if declared != expected:
        raise ValidationError(
            f"{record_label} intent_refs do not exactly match its source Runs"
        )
    return expected
