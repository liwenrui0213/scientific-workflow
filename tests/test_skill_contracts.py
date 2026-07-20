from __future__ import annotations

from collections import Counter
import copy
from difflib import SequenceMatcher
import json
from pathlib import Path, PurePosixPath
import re
import unittest

from tools.studyctl.validation import validate_schema_instance


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPOSITORY_ROOT / ".agents" / "skills"
WORKFLOW_GUIDE = REPOSITORY_ROOT / "docs" / "scientific-agent-workflow.md"
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "skill_contracts"

SKILL_NAMES = (
    "bootstrap-scientific-workflow",
    "start-scientific-study",
    "scientific-study",
    "research-compaction",
    "scientific-review",
)

REQUIRED_HEADINGS = (
    "Authoritative inputs",
    "Workflow",
    "Hard gates",
    "Output and handoff",
)

EXPECTED_REFERENCES = {
    "bootstrap-scientific-workflow": set(),
    "start-scientific-study": {"references/alignment-cases.md"},
    "scientific-study": {"references/research-strategy.md"},
    "research-compaction": {"references/semantic-compaction.md"},
    "scientific-review": {"references/adversarial-review-rubric.md"},
}

DESCRIPTION_REQUIREMENTS = {
    "bootstrap-scientific-workflow": ("install", "adapt", "scientific workflow"),
    "start-scientific-study": ("natural-language", "new", "study draft"),
    "scientific-study": ("existing", "approved", "start-scientific-study"),
    "research-compaction": ("compact", "finite active context", "without deleting history"),
    "scientific-review": ("independently", "falsify", "human verdict"),
}

HANDOFF_REQUIREMENTS = {
    "bootstrap-scientific-workflow": ("report", "validation"),
    "start-scientific-study": ("approval", "scientific-study"),
    "scientific-study": ("research-compaction", "scientific-review"),
    "research-compaction": ("checkpoint", "evidence"),
    "scientific-review": ("human", "verdict"),
}

MAX_SKILL_WORDS = {
    "bootstrap-scientific-workflow": 750,
    "start-scientific-study": 900,
    "scientific-study": 900,
    "research-compaction": 700,
    "scientific-review": 700,
}


def parse_skill(path: Path) -> tuple[dict[str, str], str]:
    """Parse the deliberately small top-level subset used by Skill frontmatter."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError(f"{path} must start with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{path} has no closing frontmatter delimiter") from exc

    metadata: dict[str, str] = {}
    current_key: str | None = None
    folded = False
    for line in lines[1:closing]:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?", line)
        if match:
            current_key = match.group(1)
            raw_value = (match.group(2) or "").strip()
            folded = raw_value in {">", ">-", "|", "|-"}
            metadata[current_key] = "" if folded else raw_value.strip("\"'")
            continue
        if current_key is None or (line and not line[0].isspace()):
            raise AssertionError(f"unsupported frontmatter line in {path}: {line!r}")
        continuation = line.strip()
        if continuation:
            separator = " " if folded and metadata[current_key] else ""
            metadata[current_key] += separator + continuation

    body = "\n".join(lines[closing + 1 :]).strip() + "\n"
    return metadata, body


def second_level_sections(body: str) -> dict[str, str]:
    headings = list(re.finditer(r"^## ([^\n]+)\s*$", body, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1).strip()] = body[start:end].strip()
    return sections


def referenced_skill_files(body: str) -> set[str]:
    return set(re.findall(r"references/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.md", body))


class SkillContractTests(unittest.TestCase):
    def test_all_five_skills_have_valid_frontmatter_and_explicit_routing(self) -> None:
        discovered = {path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md")}
        self.assertEqual(discovered, set(SKILL_NAMES))

        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                metadata, body = parse_skill(SKILLS_ROOT / skill_name / "SKILL.md")
                self.assertEqual(metadata.get("name"), skill_name)
                description = metadata.get("description", "")
                self.assertGreaterEqual(len(description.split()), 8)
                lowered = description.casefold()
                for phrase in DESCRIPTION_REQUIREMENTS[skill_name]:
                    self.assertIn(phrase.casefold(), lowered)
                self.assertRegex(body, rf"(?m)^# (?!#).+\S$")

    def test_every_skill_exposes_inputs_workflow_gates_and_handoff(self) -> None:
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                _, body = parse_skill(SKILLS_ROOT / skill_name / "SKILL.md")
                sections = second_level_sections(body)
                for heading in REQUIRED_HEADINGS:
                    self.assertIn(heading, sections)
                    self.assertGreaterEqual(
                        len(sections[heading].split()),
                        8,
                        f"{skill_name} section {heading!r} is too weak to be actionable",
                    )
                handoff = sections["Output and handoff"].casefold()
                for phrase in HANDOFF_REQUIREMENTS[skill_name]:
                    self.assertIn(phrase.casefold(), handoff)
                self.assertNotRegex(body, r"(?im)^\s*(?:TODO|TBD)\s*:\s*$|<placeholder>")

    def test_direct_references_are_one_level_linked_and_exist(self) -> None:
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                skill_dir = SKILLS_ROOT / skill_name
                _, body = parse_skill(skill_dir / "SKILL.md")
                references = referenced_skill_files(body)
                self.assertEqual(references, EXPECTED_REFERENCES[skill_name])
                for reference in references:
                    relative = PurePosixPath(reference)
                    self.assertEqual(
                        len(relative.parts),
                        2,
                        f"{skill_name} must link references directly, not through a deep tree",
                    )
                    target = skill_dir.joinpath(*relative.parts)
                    self.assertTrue(target.is_file(), f"missing linked reference: {target}")
                    self.assertGreaterEqual(len(target.read_text(encoding="utf-8").split()), 20)

    def test_every_skill_has_an_agent_interface_that_invokes_the_right_skill(self) -> None:
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                interface_path = SKILLS_ROOT / skill_name / "agents" / "openai.yaml"
                self.assertTrue(interface_path.is_file(), f"missing {interface_path}")
                interface = interface_path.read_text(encoding="utf-8")
                self.assertRegex(interface, r"(?m)^interface:\s*$")
                self.assertRegex(interface, r"(?m)^\s+display_name:\s*\S")
                self.assertRegex(interface, r"(?m)^\s+short_description:\s*\S")
                prompt_match = re.search(
                    r"(?m)^\s+default_prompt:\s*[\"']?([^\n\"']+)",
                    interface,
                )
                self.assertIsNotNone(prompt_match, f"missing default_prompt in {interface_path}")
                self.assertIn(f"${skill_name}", prompt_match.group(1))

    def test_skills_remain_thin_and_do_not_copy_the_workflow_guide(self) -> None:
        guide_lines = [
            line.strip()
            for line in WORKFLOW_GUIDE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for skill_name in SKILL_NAMES:
            with self.subTest(skill=skill_name):
                skill_text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(
                    encoding="utf-8"
                )
                self.assertLessEqual(len(skill_text.split()), MAX_SKILL_WORDS[skill_name])
                skill_lines = [line.strip() for line in skill_text.splitlines() if line.strip()]
                longest_copy = SequenceMatcher(
                    a=guide_lines,
                    b=skill_lines,
                    autojunk=False,
                ).find_longest_match()
                self.assertLess(
                    longest_copy.size,
                    8,
                    f"{skill_name} copies a long contiguous block from the workflow guide",
                )


class SkillPressureScenarioFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (FIXTURES_ROOT / "pressure-scenarios.schema.json").read_text(encoding="utf-8")
        )
        cls.catalog = json.loads(
            (FIXTURES_ROOT / "pressure-scenarios.json").read_text(encoding="utf-8")
        )

    def assert_valid_catalog(self, catalog: object) -> None:
        self.assertIsInstance(catalog, dict)
        self.assertEqual(set(catalog), {"schema_version", "scenarios"})
        self.assertEqual(catalog["schema_version"], 1)
        self.assertIsInstance(catalog["scenarios"], list)
        self.assertGreaterEqual(len(catalog["scenarios"]), len(SKILL_NAMES))
        expected_fields = {
            "id",
            "skill",
            "pressure_sources",
            "prompt",
            "expected_actions",
            "forbidden_actions",
            "invariant_under_test",
        }
        ids: list[str] = []
        for scenario in catalog["scenarios"]:
            self.assertIsInstance(scenario, dict)
            self.assertEqual(set(scenario), expected_fields)
            self.assertRegex(scenario["id"], r"^[a-z0-9-]+-[0-9]{2}$")
            ids.append(scenario["id"])
            self.assertIn(scenario["skill"], SKILL_NAMES)
            for list_field in ("pressure_sources", "expected_actions", "forbidden_actions"):
                self.assertIsInstance(scenario[list_field], list)
                self.assertGreaterEqual(len(scenario[list_field]), 1)
                self.assertTrue(
                    all(isinstance(item, str) and item.strip() for item in scenario[list_field])
                )
            self.assertEqual(
                len(scenario["pressure_sources"]),
                len(set(scenario["pressure_sources"])),
            )
            for text_field in ("prompt", "invariant_under_test"):
                self.assertIsInstance(scenario[text_field], str)
                self.assertGreaterEqual(len(scenario[text_field].split()), 5)
        self.assertEqual(len(ids), len(set(ids)), "pressure scenario IDs must be unique")

    def test_fixture_schema_is_strict_and_names_all_skills(self) -> None:
        self.assertEqual(self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(self.schema["additionalProperties"])
        scenario_schema = self.schema["properties"]["scenarios"]["items"]
        self.assertFalse(scenario_schema["additionalProperties"])
        self.assertEqual(set(scenario_schema["properties"]["skill"]["enum"]), set(SKILL_NAMES))
        self.assertEqual(
            set(scenario_schema["required"]),
            {
                "id",
                "skill",
                "pressure_sources",
                "prompt",
                "expected_actions",
                "forbidden_actions",
                "invariant_under_test",
            },
        )

    def test_pressure_catalog_is_well_formed_and_covers_every_skill(self) -> None:
        self.assertEqual(validate_schema_instance(self.catalog, self.schema), [])
        self.assert_valid_catalog(self.catalog)
        coverage = Counter(item["skill"] for item in self.catalog["scenarios"])
        self.assertEqual(set(coverage), set(SKILL_NAMES))
        self.assertTrue(all(count >= 2 for count in coverage.values()), coverage)

    def test_fixture_schema_rejects_empty_and_malformed_scenarios(self) -> None:
        empty = {"schema_version": 1, "scenarios": []}
        empty_issues = validate_schema_instance(empty, self.schema)
        self.assertTrue(
            any("expected at least 5 item(s)" in issue for issue in empty_issues),
            empty_issues,
        )

        malformed = copy.deepcopy(self.catalog)
        del malformed["scenarios"][0]["forbidden_actions"]
        malformed_issues = validate_schema_instance(malformed, self.schema)
        self.assertTrue(
            any("missing required property 'forbidden_actions'" in issue for issue in malformed_issues),
            malformed_issues,
        )


if __name__ == "__main__":
    unittest.main()
