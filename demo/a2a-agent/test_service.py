import json
import os
import unittest
from unittest import mock

from fractions import Fraction

from curriculum_index import CURRICULUM
from generators import build_quiz
from questions import Question, is_correct, parse_int
from quiz_id import ID_CHARS, KINDS, LEVELS, MAX_SEED, InvalidQuizId, QuizIdCodec, QuizSpec, normalise
from service import QuizService

KEY = "unit-test-signing-key-0123456789-abcdef"


def make_service(model=None, seed=1234):
    return QuizService(KEY, model, seed_factory=lambda: seed)


async def call(service, **request):
    return json.loads(await service.handle_text(json.dumps(request)))


def question(match, expected, qtype="integer", choices=None, display=""):
    return Question("q1", "obj", qtype, "prompt", "fmt", match, expected, display, "why", choices)


class MatcherTests(unittest.TestCase):
    def test_simplify_requires_lowest_terms_but_value_questions_accept_equivalents(self):
        simplify = question("frac_lowest", Fraction(3, 4), "fraction")
        for answer in ("3/4", " 3 / 4 ", "3\u20444", "3\u22154"):
            self.assertTrue(is_correct(simplify, answer), answer)
        for answer in ("6/8", "0.75", "3/-4", "-3/-4", "3/0", "", "three quarters", "3/4/5"):
            self.assertFalse(is_correct(simplify, answer), answer)
        value = question("frac_value", Fraction(3, 4), "fraction")
        for answer in ("6/8", "3/4", "-6/-8", "3/4 "):
            self.assertTrue(is_correct(value, answer), answer)
        self.assertFalse(is_correct(value, "3/5"))
        whole = question("frac_value", Fraction(2), "fraction")
        self.assertTrue(is_correct(whole, "2") and is_correct(whole, "4/2"))
        self.assertFalse(is_correct(question("frac_lowest", Fraction(2), "fraction"), "4/2"))
        self.assertTrue(is_correct(question("frac_lowest", Fraction(2), "fraction"), "2"))

    def test_exact_fraction_keeps_the_requested_denominator(self):
        exact = question("frac_exact", (10, 15), "fraction")
        self.assertTrue(is_correct(exact, "10/15"))
        self.assertFalse(is_correct(exact, "2/3"))

    def test_integers_normalise_signs_and_reject_separators_and_decimals(self):
        negative = question("int", -5)
        for answer in ("-5", "\u22125", " -5 ", "\u2212 5", "-\u00a05"):
            self.assertTrue(is_correct(negative, answer), repr(answer))
        self.assertTrue(is_correct(question("int", 5), "+5"))
        for answer in ("5.0", "1,000", "5 5", "five", "", "5/1", "0x5"):
            self.assertFalse(is_correct(question("int", 5), answer), answer)
        self.assertFalse(is_correct(question("int", 1000), "1,000"))
        self.assertIsNone(parse_int("1,000"))
        self.assertFalse(is_correct(question("int", 5), "9" * 40))

    def test_equation_answers_accept_the_variable_prefix(self):
        equation = question("int_var:x", 5)
        for answer in ("5", "x=5", "x = 5", "X = 5", " x= 5 "):
            self.assertTrue(is_correct(equation, answer), answer)
        for answer in ("y=5", "x=6", "x == 5", "5x", "x=", "=5"):
            self.assertFalse(is_correct(equation, answer), answer)
        self.assertTrue(is_correct(question("int_var:n", -3), "n = \u22123"))

    def test_prime_factor_lists_ignore_order_and_accept_products(self):
        factors = question("int_list", [2, 2, 3], "integer_list")
        for answer in ("2, 2, 3", "3,2,2", "2*2*3", "2\u00d72\u00d73", "2 2 3", "2^2*3", "2^2, 3", "2 \u00b7 2 \u00b7 3"):
            self.assertTrue(is_correct(factors, answer), answer)
        for answer in ("12", "2, 3", "2,2,3,3", "2*3", "4, 3", "2^2^3", "", "a, b", "2, 2, 3x"):
            self.assertFalse(is_correct(factors, answer), answer)

    def test_choices_match_by_letter_or_text(self):
        pick = question("choice", 1, "choice", ("3/4", "9/12", "5/7", "1/2"))
        for answer in ("B", "b", "B)", "(b)", "b.", " B ", "9/12", " 9 / 12 "):
            self.assertTrue(is_correct(pick, answer), answer)
        for answer in ("A", "C", "E", "AB", "b b", "", "9/13", "2"):
            self.assertFalse(is_correct(pick, answer), answer)
        yes_no = question("choice", 1, "choice", ("Yes", "No"))
        self.assertTrue(is_correct(yes_no, "no") and is_correct(yes_no, "B"))
        self.assertFalse(is_correct(yes_no, "C"))  # there is no third option
        self.assertFalse(is_correct(yes_no, "yes"))


class ChoiceFormTests(unittest.TestCase):
    def test_the_form_shown_to_learners_is_accepted_only_when_letter_and_text_agree(self):
        q = question("choice", 2, "choice", ("1/2", "3/4", "3/12", "4/8"))
        for accepted in ("C", "c)", "3/12", "C) 3/12", "c) 3/12", "(C) 3/12", "C. 3/12", "C)3/12", "C) "):
            self.assertTrue(is_correct(q, accepted), accepted)
        for rejected in ("B) 3/12", "C) 3/4", "D) 3/12", "E) 3/12", "3/4", ""):
            self.assertFalse(is_correct(q, rejected), rejected)


class RoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_expected_answer_the_agent_reports_is_accepted_when_submitted_back(self):
        # Regression: choice questions reported "C) text" but rejected that exact string.
        for topic, sections in CURRICULUM.items():
            for section in sections:
                for level in ("intro", "working", "deep"):
                    for seed in range(8):
                        service = make_service(seed=seed)
                        quiz = await call(service, action="create", kind="mastery", topic_id=topic,
                                          section_id=section, level=level, count=8)
                        wrong = {q["id"]: "zzz" for q in quiz["questions"]}
                        graded = await call(service, action="grade", quiz_id=quiz["quiz_id"], answers=wrong)
                        expected = {r["id"]: r["expected"] for r in graded["results"]}
                        again = await call(service, action="grade", quiz_id=quiz["quiz_id"], answers=expected)
                        self.assertEqual((again["score"], again["passed"]), (again["max"], True),
                                         (topic, section, level, seed))


class QuizIdTests(unittest.TestCase):
    ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

    def setUp(self):
        self.codec = QuizIdCodec(KEY)
        self.spec = QuizSpec("section", "fractions", "fractions-basics", "working", 5, 123456789012)
        self.quiz_id = self.codec.encode(self.spec)
        self.compact = self.quiz_id.replace("-", "")

    def test_round_trip_and_short_copyable_format(self):
        self.assertEqual(self.codec.decode(self.quiz_id), self.spec)
        self.assertRegex(self.quiz_id, r"^([0-9a-hjkmnp-tv-z]{4}-){5}[0-9a-hjkmnp-tv-z]{4}$")
        self.assertEqual(len(self.compact), ID_CHARS)
        self.assertLessEqual(len(self.quiz_id), 30)
        for forbidden in ("fractions", "working", "answer", "eyJ"):
            self.assertNotIn(forbidden, self.quiz_id)

    def test_every_spec_round_trips(self):
        for kind in KINDS:
            for topic, sections in CURRICULUM.items():
                for section in sections:
                    for level in LEVELS:
                        for count in (3, 10):
                            for seed in (0, 1, MAX_SEED - 1):
                                spec = QuizSpec(kind, topic, None if kind == "diagnostic" else section, level, count, seed)
                                self.assertEqual(self.codec.decode(self.codec.encode(spec)), spec)

    def test_tolerates_what_models_and_humans_do_when_copying(self):
        variants = [
            self.quiz_id.upper(), self.compact, self.quiz_id.replace("-", " "), f" {self.quiz_id} ", f'"{self.quiz_id}"',
            f"`{self.quiz_id}`", f"({self.quiz_id}).", f"{self.quiz_id}\n", self.quiz_id.replace("-", "_"),
            "-".join(self.compact[i : i + 6] for i in range(0, 24, 6)),
        ]
        for variant in variants:
            self.assertEqual(self.codec.decode(variant), self.spec, repr(variant))

    def test_ambiguous_characters_decode_to_the_digit_they_resemble(self):
        # Crockford: o/O read as 0 and i/I/l/L read as 1, so a mis-seen digit still verifies.
        for original, alias in (("0", "o"), ("1", "i"), ("1", "l")):
            if original in self.compact:
                self.assertEqual(self.codec.decode(self.compact.replace(original, alias)), self.spec)

    def test_every_single_character_change_is_rejected(self):
        aliases = {"0": "o", "1": "il"}
        for index, original in enumerate(self.compact):
            for replacement in self.ALPHABET:
                if replacement == original or replacement in aliases.get(original, "") or original in aliases.get(replacement, ""):
                    continue
                forged = self.compact[:index] + replacement + self.compact[index + 1 :]
                with self.assertRaises(InvalidQuizId, msg=f"index {index} -> {replacement!r}"):
                    self.codec.decode(forged)

    def test_truncated_extended_and_malformed_ids_are_rejected(self):
        candidates = [
            self.compact[:-1], self.compact[:-5], self.compact + "0", self.compact + "k3x9", self.compact[:12],
            "", "-", "abc", "\u00e9" + self.compact, "u" * 24, "!" * 24, self.compact[:-1] + "u", "A" * 600,
            None, 5, ["x"], {"a": 1}, self.quiz_id + " " + self.quiz_id,
        ]
        for candidate in candidates:
            with self.assertRaises(InvalidQuizId, msg=repr(candidate)[:60]):
                self.codec.decode(candidate)

    def test_wrong_key_is_rejected(self):
        with self.assertRaises(InvalidQuizId):
            QuizIdCodec("another-signing-key-0123456789-abcdefgh").decode(self.quiz_id)

    def test_a_changed_curriculum_invalidates_older_ids(self):
        import quiz_id as module

        with mock.patch.dict(module.CURRICULUM, {"zero": {"zero-basics": ["zero.one"]}}, clear=False):
            with self.assertRaises(InvalidQuizId):
                QuizIdCodec(KEY).decode(self.quiz_id)

    def test_correctly_tagged_but_malformed_payloads_are_rejected(self):
        from quiz_id import _decode, _encode

        good = _decode(normalise(self.quiz_id))[:10]
        spare_bits = bytes([good[0], good[1] | 1]) + good[2:]
        bad_payloads = {
            "spare bits": spare_bits,
            "version": bytes([good[0] ^ 0x20]) + good[1:],
            "kind 3": bytes([good[0] | 0x18]) + good[1:],
            "level 3": bytes([good[0] | 0x06]) + good[1:],
            "count over 10": bytes([good[0] | 0x01, good[1] & 0x1F]) + good[2:],  # count field = 8, i.e. 11 questions
            "unknown topic": good[:2] + bytes([200]) + good[3:],
            "unknown section": good[:3] + bytes([100]) + good[4:],
            "no section for a section quiz": good[:3] + bytes([0xFF]) + good[4:],
        }
        for name, payload in bad_payloads.items():
            forged = _encode(payload + self.codec._tag(payload))
            with self.assertRaises(InvalidQuizId, msg=name):
                self.codec.decode(forged)



class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def create(self, service=None, **overrides):
        request = {"action": "create", "kind": "section", "topic_id": "fractions", "section_id": "fractions-basics", **overrides}
        return await call(service or make_service(), **request)

    def correct_answers(self, quiz_id, wrong=()):
        spec = QuizIdCodec(KEY).decode(quiz_id)
        answers = {}
        for q in build_quiz(spec.kind, spec.topic, spec.section, spec.level, spec.count, spec.seed):
            answer = "ABCD"[q.expected] if q.type == "choice" else q.display.split(" = ")[-1]
            answers[q.id] = "no idea" if q.id in wrong else answer
        return answers

    async def test_create_reply_shape_and_defaults(self):
        reply = await self.create()
        self.assertEqual(
            set(reply), {"quiz_id", "kind", "topic_id", "section_id", "level", "questions", "model_assisted"},
        )
        self.assertEqual((reply["kind"], reply["topic_id"], reply["section_id"], reply["level"]), ("section", "fractions", "fractions-basics", "working"))
        self.assertEqual(len(reply["questions"]), 5)
        self.assertIs(reply["model_assisted"], False)
        for q in reply["questions"]:
            self.assertEqual(set(q) - {"choices"}, {"id", "objective", "type", "prompt", "answer_format"})
            self.assertEqual("choices" in q, q["type"] == "choice")
            self.assertIn(q["type"], {"fraction", "integer", "integer_list", "choice"})
        mastery = await self.create(kind="mastery")
        self.assertEqual(len(mastery["questions"]), 8)
        diagnostic = await call(make_service(), action="create", kind="diagnostic", topic_id="primes")
        self.assertEqual((len(diagnostic["questions"]), diagnostic["section_id"]), (5, None))
        self.assertEqual(
            [q["objective"] for q in diagnostic["questions"]],
            ["prime.identify", "prime.divisibility", "prime.prime-factorization", "prime.gcd", "prime.lcm"],
        )

    async def test_create_and_grade_round_trip_with_perfect_and_wrong_answers(self):
        service = make_service()
        reply = await self.create(service, count=6)
        perfect = await call(service, action="grade", quiz_id=reply["quiz_id"], answers=self.correct_answers(reply["quiz_id"]))
        self.assertEqual(
            set(perfect),
            {"quiz_id", "kind", "score", "max", "percent", "passed", "pass_threshold", "results", "weak_objectives", "summary", "model_assisted"},
        )
        self.assertEqual((perfect["score"], perfect["max"], perfect["percent"], perfect["passed"]), (6, 6, 100.0, True))
        self.assertEqual(perfect["weak_objectives"], [])
        for result in perfect["results"]:
            self.assertEqual(set(result), {"id", "objective", "correct"})

        graded = await call(service, action="grade", quiz_id=reply["quiz_id"], answers=self.correct_answers(reply["quiz_id"], wrong=("q1", "q3")))
        self.assertEqual((graded["score"], graded["percent"]), (4, 66.7))
        self.assertEqual(graded["pass_threshold"], 0.6)
        self.assertIs(graded["passed"], True)
        by_id = {r["id"]: r for r in graded["results"]}
        for qid in ("q1", "q3"):
            self.assertFalse(by_id[qid]["correct"])
            self.assertTrue(by_id[qid]["expected"] and by_id[qid]["explanation"])
        for qid in ("q2", "q4", "q5", "q6"):
            self.assertEqual(set(by_id[qid]), {"id", "objective", "correct"})
        self.assertEqual(graded["weak_objectives"], ["frac.equivalent"])
        self.assertIn("review frac.equivalent", graded["summary"])

    async def test_thresholds_per_kind(self):
        service = make_service()
        cases = [
            ("section", 5, 3, 0.6, True), ("section", 5, 2, 0.6, False),
            ("mastery", 5, 4, 0.8, True), ("mastery", 5, 3, 0.8, False),
            ("diagnostic", 5, 0, None, None), ("diagnostic", 5, 5, None, None),
        ]
        for kind, count, right, threshold, passed in cases:
            with self.subTest(kind=kind, right=right):
                request = {"kind": kind, "topic_id": "primes", "count": count}
                if kind != "diagnostic":
                    request["section_id"] = "primes-basics"
                reply = await call(service, action="create", **request)
                wrong = [f"q{i}" for i in range(right + 1, count + 1)]
                graded = await call(service, action="grade", quiz_id=reply["quiz_id"], answers=self.correct_answers(reply["quiz_id"], wrong))
                self.assertEqual(graded["score"], right)
                self.assertEqual(graded["pass_threshold"], threshold)
                self.assertEqual(graded["passed"], passed)

    async def test_retakes_differ_but_a_quiz_id_always_grades_the_same_quiz(self):
        service = QuizService(KEY)
        first = json.loads(await service.handle_text(json.dumps({"action": "create", "kind": "section", "topic_id": "primes", "section_id": "primes-gcd-lcm"})))
        second = json.loads(await service.handle_text(json.dumps({"action": "create", "kind": "section", "topic_id": "primes", "section_id": "primes-gcd-lcm"})))
        self.assertNotEqual(first["quiz_id"], second["quiz_id"])
        self.assertNotEqual(first["questions"], second["questions"])
        again = await call(service, action="grade", quiz_id=first["quiz_id"], answers=self.correct_answers(first["quiz_id"]))
        self.assertEqual(again["score"], 5)

    async def test_create_validation_errors(self):
        service = make_service()
        base = {"action": "create", "kind": "section", "topic_id": "fractions", "section_id": "fractions-basics"}
        cases = [
            ({"kind": "exam"}, "invalid_request"),
            ({"kind": None}, "invalid_request"),
            ({"topic_id": "algebra"}, "unknown_topic"),
            ({"topic_id": 5}, "invalid_request"),
            ({"topic_id": None}, "invalid_request"),
            ({"section_id": "primes-basics"}, "unknown_section"),
            ({"section_id": None}, "invalid_request"),
            ({"section_id": ""}, "invalid_request"),
            ({"kind": "mastery", "section_id": "nope"}, "unknown_section"),
            ({"kind": "diagnostic"}, "invalid_request"),  # diagnostics forbid section_id
            ({"level": "expert"}, "invalid_request"),
            ({"count": 2}, "invalid_request"),
            ({"count": 11}, "invalid_request"),
            ({"count": "5"}, "invalid_request"),
            ({"count": 5.0}, "invalid_request"),
            ({"count": True}, "invalid_request"),
            ({"count": None, "level": None}, "invalid_request"),
            ({"seed": 5}, "invalid_request"),  # the seed is never accepted from the wire
            ({"extra": 1}, "invalid_request"),
        ]
        for override, code in cases:
            with self.subTest(override=override):
                reply = await call(service, **{**base, **override})
                self.assertEqual(set(reply), {"error"})
                self.assertEqual(reply["error"]["code"], code)
                self.assertTrue(reply["error"]["message"])
                self.assertNotIn("Traceback", reply["error"]["message"])
        ok = await call(service, **{**base, "count": 10})
        self.assertEqual(len(ok["questions"]), 10)
        self.assertEqual((await call(service, **{**base, "count": 3}))["questions"].__len__(), 3)

    async def test_request_text_errors(self):
        service = make_service()
        for text in ("hello", "", "[1,2]", "5", "null", '{"action":"dance"}', "{}", '{"action":null}', "{", "\x00"):
            reply = json.loads(await service.handle_text(text))
            self.assertEqual(reply["error"]["code"], "invalid_request", text)
        huge = json.dumps({"action": "grade", "quiz_id": "x", "answers": {"q1": "a" * 9000}})
        self.assertEqual(json.loads(await service.handle_text(huge))["error"]["code"], "invalid_request")

    async def test_grade_validation_errors(self):
        service = make_service()
        reply = await self.create(service, count=3)
        answers = self.correct_answers(reply["quiz_id"])
        quiz_id = reply["quiz_id"]
        cases = {
            "missing": ({"q1": "1", "q2": "1"}, "invalid_request"),
            "extra": ({**answers, "q4": "1"}, "invalid_request"),
            "swapped id": ({"q1": "1", "q2": "1", "q9": "1"}, "invalid_request"),
            "not a dict": (["1", "2", "3"], "invalid_request"),
            "none": (None, "invalid_request"),
            "bad value": ({**answers, "q1": ["3/4"]}, "invalid_request"),
            "bool value": ({**answers, "q1": True}, "invalid_request"),
            "null value": ({**answers, "q1": None}, "invalid_request"),
            "too long": ({**answers, "q1": "9" * 65}, "invalid_request"),
        }
        for name, (value, code) in cases.items():
            with self.subTest(name):
                graded = await call(service, action="grade", quiz_id=quiz_id, answers=value)
                self.assertEqual(graded["error"]["code"], code)
        self.assertEqual((await call(service, action="grade", quiz_id=quiz_id))["error"]["code"], "invalid_request")
        self.assertEqual((await call(service, action="grade", quiz_id=quiz_id, answers=answers, extra=1))["error"]["code"], "invalid_request")
        for bad_id in (None, 7, "", "abc", quiz_id[:-2], quiz_id + "x", "x" * 24):
            graded = await call(service, action="grade", quiz_id=bad_id, answers=answers)
            self.assertEqual(graded["error"]["code"], "invalid_quiz_id", repr(bad_id))
        # skipping a question with an empty answer is allowed; integers are accepted for numeric answers
        skipped = await call(service, action="grade", quiz_id=quiz_id, answers={**answers, "q1": ""})
        self.assertFalse(skipped["results"][0]["correct"])
        self.assertEqual((await call(service, action="grade", quiz_id=quiz_id, answers={**answers, "q1": 64}))["results"][0]["id"], "q1")
        self.assertEqual(
            (await call(service, action="grade", quiz_id=quiz_id, answers={**answers, "q1": "9" * 64}))["max"], 3,
        )

    async def test_quiz_id_from_another_key_is_not_accepted(self):
        reply = await self.create()
        other = QuizService("a-completely-different-key-0123456789abcdef", seed_factory=lambda: 1)
        graded = await call(other, action="grade", quiz_id=reply["quiz_id"], answers=self.correct_answers(reply["quiz_id"]))
        self.assertEqual(graded["error"]["code"], "invalid_quiz_id")

    async def test_create_replies_never_reveal_answers(self):
        forbidden = {"answer", "answers", "expected", "explanation", "solution", "correct", "display", "match"}

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)

        codec = QuizIdCodec(KEY)
        checked = 0
        for topic, sections in CURRICULUM.items():
            for section in sections:
                for level in ("intro", "working", "deep"):
                    for seed in range(15):
                        service = make_service(seed=seed)
                        text = await service.handle_text(json.dumps({"action": "create", "kind": "section", "topic_id": topic, "section_id": section, "level": level, "count": 10}))
                        reply = json.loads(text)
                        self.assertFalse(forbidden & set(keys(reply)), text)
                        self.assertLessEqual(len(reply["quiz_id"]), 30)
                        self.assertEqual(set(reply), {"quiz_id", "kind", "topic_id", "section_id", "level", "questions", "model_assisted"})
                        spec = codec.decode(reply["quiz_id"])
                        for public, q in zip(reply["questions"], build_quiz(spec.kind, spec.topic, spec.section, spec.level, spec.count, spec.seed)):
                            visible = json.dumps(public, ensure_ascii=False)
                            if q.type in ("fraction", "integer_list") and q.match != "frac_exact":
                                # A non-trivial answer token ("5/7", "2, 2, 3") must not be readable from the question
                                # unless the question itself gives that very value as an operand.
                                given = set(q.facts) | {t for t in q.prompt.replace(",", " ").split()}
                                if q.display not in given and not any(q.display in fact for fact in q.facts):
                                    self.assertNotIn(q.display, visible, q.prompt)
                            if q.type != "choice":
                                self.assertNotIn("choices", public)
                            checked += 1
        self.assertGreater(checked, 1000)


class StartupTests(unittest.TestCase):
    def test_missing_or_short_signing_key_fails_fast_with_a_clear_message(self):
        from app import create_app

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "QUIZ_SIGNING_KEY is required"):
                create_app()
        with mock.patch.dict(os.environ, {"QUIZ_SIGNING_KEY": "too-short"}, clear=True):
            with self.assertRaisesRegex(ValueError, "at least 32"):
                create_app()
        with mock.patch.dict(os.environ, {"QUIZ_SIGNING_KEY": KEY}, clear=True):
            create_app()
        create_app(signing_key=KEY)


if __name__ == "__main__":
    unittest.main()
