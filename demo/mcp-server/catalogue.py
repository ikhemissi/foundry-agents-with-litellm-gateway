"""Loads and validates the read-only curriculum catalogue (JSON files under ./curriculum).

Validation is strict and runs at import time of the server, so a bad data file fails fast with a clear message.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CURRICULUM_DIR = Path(__file__).parent / "curriculum"
MODES_FILE = "modes.json"

DEPTHS = ("intro", "working", "deep")
QUIZ_KINDS = ("diagnostic", "section", "mastery")
QUIZ_WHEN = ("after_each_section", "on_request", "before_advancing")

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
OBJECTIVE_ID_RE = re.compile(r"^[a-z]+\.[a-z0-9-]+$")
MODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")

MAX_EXPLANATION_WORDS = 120
MAX_SHORT_TEXT = 160
MAX_STEP_TEXT = 240
MAX_QUESTION_TEXT = 200


class CurriculumError(ValueError):
    """Raised when curriculum data is malformed."""


@dataclass(frozen=True)
class Catalogue:
    topics: dict[str, dict[str, Any]]
    modes: list[dict[str, Any]]


def _fail(where: str, message: str) -> None:
    raise CurriculumError(f"{where}: {message}")


def _keys(obj: Any, required: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        _fail(where, "must be an object")
    missing, extra = required - obj.keys(), obj.keys() - required
    if missing:
        _fail(where, f"missing keys {sorted(missing)}")
    if extra:
        _fail(where, f"unexpected keys {sorted(extra)}")
    return obj


def _text(value: Any, where: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(where, "must be a non-empty string")
    if len(value) > max_len:
        _fail(where, f"is {len(value)} characters, limit is {max_len}")
    return value


def _text_list(value: Any, where: str, low: int, high: int, max_len: int) -> list[str]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        _fail(where, f"must be a list of {low}-{high} strings")
    for i, item in enumerate(value):
        _text(item, f"{where}[{i}]", max_len)
    return value


def _id(value: Any, where: str, pattern: re.Pattern[str] = ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(where, f"id {str(value)[:40]!r} does not match {pattern.pattern}")
    return value


def _validate_depth_block(block: Any, where: str) -> None:
    _keys(block, {"explanation", "key_points", "worked_example", "common_mistakes", "check_questions"}, where)
    explanation = _text(block["explanation"], f"{where}.explanation", 1200)
    words = len(explanation.split())
    if words > MAX_EXPLANATION_WORDS:
        _fail(f"{where}.explanation", f"has {words} words, limit is {MAX_EXPLANATION_WORDS}")
    _text_list(block["key_points"], f"{where}.key_points", 3, 5, MAX_SHORT_TEXT)
    example = _keys(block["worked_example"], {"problem", "steps", "answer"}, f"{where}.worked_example")
    _text(example["problem"], f"{where}.worked_example.problem", MAX_SHORT_TEXT)
    _text_list(example["steps"], f"{where}.worked_example.steps", 2, 8, MAX_STEP_TEXT)
    _text(example["answer"], f"{where}.worked_example.answer", MAX_SHORT_TEXT)
    _text_list(block["common_mistakes"], f"{where}.common_mistakes", 2, 3, MAX_SHORT_TEXT)
    questions = _text_list(block["check_questions"], f"{where}.check_questions", 2, 2, MAX_QUESTION_TEXT)
    for i, question in enumerate(questions):
        if not question.rstrip().endswith("?"):
            _fail(f"{where}.check_questions[{i}]", "must be a question ending with '?'")


def _validate_section(section: Any, where: str) -> None:
    _keys(section, {"id", "title", "summary", "prerequisites", "objectives", "estimated_minutes", "content"}, where)
    where = f"{where}[{_id(section['id'], where + '.id')}]"
    _text(section["title"], f"{where}.title", 80)
    _text(section["summary"], f"{where}.summary", MAX_SHORT_TEXT)
    prerequisites = section["prerequisites"]
    if not isinstance(prerequisites, list):
        _fail(f"{where}.prerequisites", "must be a list of section ids")
    for i, prerequisite in enumerate(prerequisites):
        _id(prerequisite, f"{where}.prerequisites[{i}]")
    objectives = section["objectives"]
    if not isinstance(objectives, list) or not 1 <= len(objectives) <= 4:
        _fail(f"{where}.objectives", "must be a list of 1-4 objectives")
    for i, objective in enumerate(objectives):
        _keys(objective, {"id", "statement"}, f"{where}.objectives[{i}]")
        _id(objective["id"], f"{where}.objectives[{i}].id", OBJECTIVE_ID_RE)
        _text(objective["statement"], f"{where}.objectives[{i}].statement", MAX_SHORT_TEXT)
    minutes = section["estimated_minutes"]
    if type(minutes) is not int or not 5 <= minutes <= 30:
        _fail(f"{where}.estimated_minutes", "must be an integer between 5 and 30")
    content = _keys(section["content"], set(DEPTHS), f"{where}.content")
    for depth in DEPTHS:
        _validate_depth_block(content[depth], f"{where}.content.{depth}")


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(node: str, path: list[str]) -> list[str] | None:
        state[node] = 1
        for nxt in graph[node]:
            if state.get(nxt) == 1:
                return [*path, node, nxt]
            if nxt not in state and (cycle := visit(nxt, [*path, node])):
                return cycle
        state[node] = 2
        return None

    for start in graph:
        if start not in state and (cycle := visit(start, [])):
            return cycle
    return None


def validate_topic(topic: Any, source: str = "topic") -> None:
    """Validate one topic dict in isolation (cross-topic checks live in build_catalogue)."""
    _keys(topic, {"id", "title", "summary", "prerequisites", "sections"}, source)
    where = f"{source}[{_id(topic['id'], source + '.id')}]"
    _text(topic["title"], f"{where}.title", 80)
    _text(topic["summary"], f"{where}.summary", MAX_SHORT_TEXT)
    if not isinstance(topic["prerequisites"], list):
        _fail(f"{where}.prerequisites", "must be a list of topic ids")
    for i, prerequisite in enumerate(topic["prerequisites"]):
        _id(prerequisite, f"{where}.prerequisites[{i}]")
    sections = topic["sections"]
    if not isinstance(sections, list) or not sections:
        _fail(f"{where}.sections", "must be a non-empty list")
    for i, section in enumerate(sections):
        _validate_section(section, f"{where}.sections[{i}]")

    ids = [s["id"] for s in sections]
    if len(set(ids)) != len(ids):
        _fail(f"{where}.sections", "duplicate section ids")
    graph: dict[str, list[str]] = {}
    for section in sections:
        for prerequisite in section["prerequisites"]:
            if prerequisite not in ids:
                _fail(f"{where}.sections[{section['id']}]", f"prerequisite {prerequisite!r} is not a section of this topic")
        graph[section["id"]] = section["prerequisites"]
    if cycle := _find_cycle(graph):
        _fail(where, f"section prerequisites form a cycle: {' -> '.join(cycle)}")


def validate_modes(data: Any, source: str = MODES_FILE) -> None:
    _keys(data, {"modes"}, source)
    modes = data["modes"]
    if not isinstance(modes, list) or not modes:
        _fail(source, "modes must be a non-empty list")
    seen: set[str] = set()
    for i, mode in enumerate(modes):
        where = f"{source}.modes[{i}]"
        _keys(mode, {"id", "title", "description", "tutor_behaviour", "quiz_policy", "default_depth"}, where)
        mode_id = _id(mode["id"], f"{where}.id", MODE_ID_RE)
        if mode_id in seen:
            _fail(where, f"duplicate mode id {mode_id!r}")
        seen.add(mode_id)
        _text(mode["title"], f"{where}.title", 80)
        _text(mode["description"], f"{where}.description", 300)
        _text_list(mode["tutor_behaviour"], f"{where}.tutor_behaviour", 3, 5, 240)
        policy = _keys(mode["quiz_policy"], {"when", "kind"}, f"{where}.quiz_policy")
        if policy["when"] not in QUIZ_WHEN:
            _fail(f"{where}.quiz_policy.when", f"must be one of {list(QUIZ_WHEN)}")
        if policy["kind"] not in QUIZ_KINDS:
            _fail(f"{where}.quiz_policy.kind", f"must be one of {list(QUIZ_KINDS)}")
        if mode["default_depth"] not in DEPTHS:
            _fail(f"{where}.default_depth", f"must be one of {list(DEPTHS)}")


def build_catalogue(topic_dicts: list[Any], modes_data: Any) -> Catalogue:
    """Validate every topic plus cross-topic rules and return the catalogue."""
    topics: dict[str, dict[str, Any]] = {}
    for topic in topic_dicts:
        validate_topic(topic)
        if topic["id"] in topics:
            _fail("catalogue", f"duplicate topic id {topic['id']!r}")
        topics[topic["id"]] = topic
    if not topics:
        _fail("catalogue", "no topics found")

    section_ids: set[str] = set()
    objective_ids: set[str] = set()
    for topic in topics.values():
        for prerequisite in topic["prerequisites"]:
            if prerequisite not in topics:
                _fail(f"topic {topic['id']}", f"prerequisite {prerequisite!r} is not a known topic")
        for section in topic["sections"]:
            if section["id"] in section_ids or section["id"] in topics:
                _fail(f"section {section['id']}", "id is not globally unique")
            section_ids.add(section["id"])
            for objective in section["objectives"]:
                if objective["id"] in objective_ids:
                    _fail(f"objective {objective['id']}", "id is not globally unique")
                objective_ids.add(objective["id"])
    if cycle := _find_cycle({t["id"]: t["prerequisites"] for t in topics.values()}):
        _fail("catalogue", f"topic prerequisites form a cycle: {' -> '.join(cycle)}")

    validate_modes(modes_data)
    return Catalogue(topics=topics, modes=modes_data["modes"])


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CurriculumError(f"{path.name}: cannot read JSON ({exc})") from exc


def load_curriculum(directory: Path = CURRICULUM_DIR) -> Catalogue:
    """Read every topic file (one per topic) plus modes.json from `directory` and validate them."""
    topic_files = sorted(p for p in directory.glob("*.json") if p.name != MODES_FILE)
    topics = []
    for path in topic_files:
        topic = _read_json(path)
        if isinstance(topic, dict) and topic.get("id") != path.stem:
            raise CurriculumError(f"{path.name}: file name must be '<topic id>.json' (topic id is {topic.get('id')!r})")
        topics.append(topic)
    return build_catalogue(topics, _read_json(directory / MODES_FILE))
