"""Quiz business logic: create and grade, independent of the A2A transport."""

import json
import secrets
from collections.abc import Callable

from curriculum_index import CURRICULUM
from enhance import rewrite_prompts, write_feedback
from generators import build_quiz
from model import QuizModel
from questions import MAX_ANSWER_CHARS, Question, is_correct
from quiz_id import KINDS, LEVELS, MAX_SEED, InvalidQuizId, QuizIdCodec, QuizSpec

MAX_INPUT_CHARS = 8192
PASS_THRESHOLDS: dict[str, float | None] = {"diagnostic": None, "section": 0.6, "mastery": 0.8}
CREATE_KEYS = frozenset({"action", "kind", "topic_id", "section_id", "level", "count"})
GRADE_KEYS = frozenset({"action", "quiz_id", "answers"})


class QuizError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _json_text(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class QuizService:
    def __init__(
        self,
        signing_key: str,
        model: QuizModel | None = None,
        seed_factory: Callable[[], int] = lambda: secrets.randbelow(MAX_SEED),
    ) -> None:
        self.codec = QuizIdCodec(signing_key)
        self.model = model
        self._seed_factory = seed_factory

    async def handle_text(self, text: str) -> str:
        """Process one request text and return the JSON reply text (errors become {"error": ...})."""
        try:
            return _json_text(await self._handle(text))
        except QuizError as error:
            return _json_text({"error": {"code": error.code, "message": error.message}})

    async def _handle(self, text: str) -> dict:
        if len(text) > MAX_INPUT_CHARS:
            raise QuizError("invalid_request", f"Input is larger than {MAX_INPUT_CHARS} characters.")
        try:
            request = json.loads(text)
        except ValueError:
            raise QuizError("invalid_request", "The message text must be a JSON object.") from None
        if not isinstance(request, dict):
            raise QuizError("invalid_request", "The message text must be a JSON object.")
        match request.get("action"):
            case "create":
                return await self._create(request)
            case "grade":
                return await self._grade(request)
        raise QuizError("invalid_request", "'action' must be 'create' or 'grade'.")

    async def _create(self, request: dict) -> dict:
        spec = self._parse_create(request)
        questions = build_quiz(spec.kind, spec.topic, spec.section, spec.level, spec.count, spec.seed)
        rewrites = await rewrite_prompts(self.model, questions) if self.model else {}
        return {
            "quiz_id": self.codec.encode(spec),
            "kind": spec.kind,
            "topic_id": spec.topic,
            "section_id": spec.section,
            "level": spec.level,
            "questions": [self._public(q, rewrites.get(q.id)) for q in questions],
            "model_assisted": bool(rewrites),
        }

    def _parse_create(self, request: dict) -> QuizSpec:
        if request.keys() - CREATE_KEYS:
            raise QuizError("invalid_request", "Unexpected field in create request.")
        kind, topic, section = request.get("kind"), request.get("topic_id"), request.get("section_id")
        level, count = request.get("level", "working"), request.get("count")
        if kind not in KINDS:
            raise QuizError("invalid_request", "'kind' must be one of: diagnostic, section, mastery.")
        if not isinstance(topic, str) or not topic:
            raise QuizError("invalid_request", "'topic_id' is required.")
        if topic not in CURRICULUM:
            raise QuizError("unknown_topic", f"Unknown topic_id. Known topics: {', '.join(CURRICULUM)}.")
        if kind == "diagnostic":
            if section is not None:
                raise QuizError("invalid_request", "'section_id' must not be set for a diagnostic quiz.")
        elif not isinstance(section, str) or not section:
            raise QuizError("invalid_request", f"'section_id' is required for a {kind} quiz.")
        elif section not in CURRICULUM[topic]:
            raise QuizError("unknown_section", f"Unknown section_id for this topic. Known sections: {', '.join(CURRICULUM[topic])}.")
        if level not in LEVELS:
            raise QuizError("invalid_request", "'level' must be one of: intro, working, deep.")
        if count is None:
            count = 8 if kind == "mastery" else 5
        if type(count) is not int or not 3 <= count <= 10:
            raise QuizError("invalid_request", "'count' must be an integer from 3 to 10.")
        return QuizSpec(kind, topic, section, level, count, self._seed_factory())

    @staticmethod
    def _public(question: Question, prompt: str | None) -> dict:
        public = {"id": question.id, "objective": question.objective, "type": question.type, "prompt": prompt or question.prompt}
        if question.choices:
            public["choices"] = list(question.choices)
        public["answer_format"] = question.answer_format
        return public

    async def _grade(self, request: dict) -> dict:
        if request.keys() - GRADE_KEYS:
            raise QuizError("invalid_request", "Unexpected field in grade request.")
        try:
            spec = self.codec.decode(request.get("quiz_id"))
        except InvalidQuizId:
            raise QuizError(
                "invalid_quiz_id",
                "The quiz id was not recognised; copy it exactly as returned by create, or create a new quiz.",
            ) from None
        questions = build_quiz(spec.kind, spec.topic, spec.section, spec.level, spec.count, spec.seed)
        answers = self._parse_answers(request.get("answers"), questions)

        results, wrong, weak = [], [], []
        for question in questions:
            correct = is_correct(question, answers[question.id])
            result = {"id": question.id, "objective": question.objective, "correct": correct}
            if not correct:
                result["expected"] = question.display
                result["explanation"] = question.explanation
                wrong.append((question, answers[question.id]))
                if question.objective not in weak:
                    weak.append(question.objective)
            results.append(result)

        feedback = await write_feedback(self.model, wrong) if self.model and wrong else {}
        for result in results:
            if result["id"] in feedback:
                result["feedback"] = feedback[result["id"]]

        score = sum(r["correct"] for r in results)
        threshold = PASS_THRESHOLDS[spec.kind]
        return {
            "quiz_id": request["quiz_id"],
            "kind": spec.kind,
            "score": score,
            "max": len(results),
            "percent": round(100 * score / len(results), 1),
            "passed": None if threshold is None else score / len(results) >= threshold,
            "pass_threshold": threshold,
            "results": results,
            "weak_objectives": weak,
            "summary": self._summary(score, len(results), threshold, weak),
            "model_assisted": bool(feedback),
        }

    @staticmethod
    def _parse_answers(answers: object, questions: list[Question]) -> dict[str, str]:
        if not isinstance(answers, dict):
            raise QuizError("invalid_request", "'answers' must be an object mapping question ids to answers.")
        expected_ids = [q.id for q in questions]
        missing = [i for i in expected_ids if i not in answers]
        if missing:
            raise QuizError("invalid_request", f"Missing answers for: {', '.join(missing)}. Send an empty string to skip a question.")
        if len(answers) != len(expected_ids):
            raise QuizError("invalid_request", f"'answers' must contain exactly the {len(expected_ids)} question ids of the quiz.")
        parsed = {}
        for question_id in expected_ids:
            value = answers[question_id]
            if type(value) is int:
                value = str(value)
            if not isinstance(value, str):
                raise QuizError("invalid_request", f"The answer for {question_id} must be a string.")
            if len(value) > MAX_ANSWER_CHARS:
                raise QuizError("invalid_request", f"The answer for {question_id} is longer than {MAX_ANSWER_CHARS} characters.")
            parsed[question_id] = value
        return parsed

    @staticmethod
    def _summary(score: int, total: int, threshold: float | None, weak: list[str]) -> str:
        text = f"You scored {score} of {total} ({round(100 * score / total, 1):g}%)"
        if threshold is not None:
            text += " and passed" if score / total >= threshold else f" and did not reach the {round(threshold * 100):g}% pass mark"
        return text + (f"; review {', '.join(weak)}." if weak else "; no weak areas found.")
