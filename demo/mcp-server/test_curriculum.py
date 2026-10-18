"""Schema and maths-correctness checks for the curriculum data (no network, no server)."""

import copy
import json
import math
import re
import unittest
from fractions import Fraction
from pathlib import Path

import catalogue
from catalogue import CurriculumError, build_catalogue, load_curriculum, validate_modes, validate_topic

DATA_DIR = Path(__file__).parent / "curriculum"

EXPECTED = {
    "fractions": {
        "fractions-basics": ["frac.equivalent", "frac.simplify"],
        "fractions-add-subtract": ["frac.common-denominator", "frac.add-sub"],
        "fractions-multiply-divide": ["frac.multiply", "frac.divide"],
    },
    "primes": {
        "primes-basics": ["prime.identify", "prime.divisibility"],
        "primes-factorization": ["prime.factor-tree", "prime.prime-factorization"],
        "primes-gcd-lcm": ["prime.gcd", "prime.lcm"],
    },
    "linear-equations": {
        "eq-one-step": ["eq.one-step", "eq.inverse-ops"],
        "eq-two-step": ["eq.two-step", "eq.negative-coeff"],
        "eq-both-sides": ["eq.variables-both-sides", "eq.check-solution"],
    },
}
EXPECTED_SECTION_PREREQS = {
    "fractions-basics": [], "fractions-add-subtract": ["fractions-basics"], "fractions-multiply-divide": ["fractions-basics"],
    "primes-basics": [], "primes-factorization": ["primes-basics"], "primes-gcd-lcm": ["primes-factorization"],
    "eq-one-step": [], "eq-two-step": ["eq-one-step"], "eq-both-sides": ["eq-two-step"],
}

SUPERSCRIPT = {"²": 2, "³": 3, "⁴": 4}


def raw_topics():
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in DATA_DIR.glob("*.json") if p.name != "modes.json"}


def raw_modes():
    return json.loads((DATA_DIR / "modes.json").read_text(encoding="utf-8"))


def all_sections():
    for topic in raw_topics().values():
        yield from topic["sections"]


def is_prime(n):
    return n > 1 and all(n % d for d in range(2, math.isqrt(n) + 1))


def to_fraction(text):
    return Fraction(text.replace("−", "-"))


def parse_factorization(text):
    """'2² × 3 × 5' -> {2: 2, 3: 1, 5: 1}; repeated bases add up."""
    result = {}
    for base, sup in re.findall(r"(\d+)([²³⁴]?)", text):
        result[int(base)] = result.get(int(base), 0) + SUPERSCRIPT.get(sup, 1)
    return result


def evaluate_side(expr, x):
    expr = expr.replace("−", "-")
    expr = re.sub(r"(?<=[\d)])(?=[x(])", "*", expr)
    expr = re.sub(r"\d+", r"F(\g<0>)", expr)
    return eval(expr, {"__builtins__": {}}, {"F": Fraction, "x": x})  # trusted repo data, test only


class CurriculumContentTests(unittest.TestCase):
    def test_exact_topics_sections_and_objectives(self):
        topics = raw_topics()
        self.assertEqual(set(topics), set(EXPECTED))
        for topic_id, sections in EXPECTED.items():
            self.assertEqual([s["id"] for s in topics[topic_id]["sections"]], list(sections))
            for section in topics[topic_id]["sections"]:
                self.assertEqual([o["id"] for o in section["objectives"]], sections[section["id"]])
                self.assertEqual(section["prerequisites"], EXPECTED_SECTION_PREREQS[section["id"]])
        self.assertEqual({t["title"] for t in topics.values()}, {"Fractions", "Prime numbers", "Linear equations"})
        self.assertTrue(all(t["prerequisites"] == [] for t in topics.values()))

    def test_real_data_loads_and_validates(self):
        loaded = load_curriculum(DATA_DIR)
        self.assertEqual(set(loaded.topics), set(EXPECTED))
        self.assertEqual([m["id"] for m in loaded.modes], ["guided_path", "map_then_zoom", "mastery_gate"])

    def test_patterns_and_global_uniqueness(self):
        id_re, obj_re = re.compile(r"^[a-z][a-z0-9-]{0,40}$"), re.compile(r"^[a-z]+\.[a-z0-9-]+$")
        objective_ids, section_ids = [], []
        for topic in raw_topics().values():
            self.assertRegex(topic["id"], id_re)
            for section in topic["sections"]:
                self.assertRegex(section["id"], id_re)
                section_ids.append(section["id"])
                for objective in section["objectives"]:
                    self.assertRegex(objective["id"], obj_re)
                    objective_ids.append(objective["id"])
        self.assertEqual(len(objective_ids), len(set(objective_ids)))
        self.assertEqual(len(section_ids), len(set(section_ids)))
        self.assertEqual(len(objective_ids), 18)

    def test_depth_blocks_complete_and_within_limits(self):
        for section in all_sections():
            self.assertEqual(set(section["content"]), {"intro", "working", "deep"})
            self.assertTrue(5 <= section["estimated_minutes"] <= 30)
            for depth, block in section["content"].items():
                with self.subTest(section=section["id"], depth=depth):
                    self.assertLessEqual(len(block["explanation"].split()), 120)
                    self.assertTrue(3 <= len(block["key_points"]) <= 5)
                    self.assertTrue(2 <= len(block["common_mistakes"]) <= 3)
                    self.assertEqual(len(block["check_questions"]), 2)
                    self.assertTrue(all(q.endswith("?") for q in block["check_questions"]))
                    self.assertEqual(set(block["worked_example"]), {"problem", "steps", "answer"})
                    self.assertLessEqual(len(json.dumps(block, ensure_ascii=False)), 2500)

    def test_prerequisites_exist_and_are_in_topic_order(self):
        for topic in raw_topics().values():
            order = [s["id"] for s in topic["sections"]]
            for index, section in enumerate(topic["sections"]):
                for prerequisite in section["prerequisites"]:
                    self.assertIn(prerequisite, order)
                    self.assertLess(order.index(prerequisite), index)

    # -- worked-example correctness -------------------------------------------------------------------------

    def example(self, section_id, depth):
        section = next(s for s in all_sections() if s["id"] == section_id)
        return section["content"][depth]["worked_example"]

    def test_equivalent_fraction(self):
        ex = self.example("fractions-basics", "intro")
        a, b, d = map(int, re.fullmatch(r".*?(\d+)/(\d+) = \?/(\d+)", ex["problem"]).groups())
        answer = re.fullmatch(r"(\d+)/(\d+)", ex["answer"])
        self.assertEqual(int(answer[2]), d)
        self.assertEqual(Fraction(int(answer[1]), d), Fraction(a, b))

    def test_simplification_examples(self):
        for depth in ("working", "deep"):
            ex = self.example("fractions-basics", depth)
            a, b = map(int, re.fullmatch(r"Simplify (\d+)/(\d+)", ex["problem"]).groups())
            g = math.gcd(a, b)
            self.assertEqual(ex["answer"], f"{a // g}/{b // g}")
        self.assertEqual(math.gcd(84, 126), 42)

    def test_fraction_arithmetic_examples(self):
        ops = {"+": lambda x, y: x + y, "−": lambda x, y: x - y, "×": lambda x, y: x * y, "÷": lambda x, y: x / y}
        for section_id in ("fractions-add-subtract", "fractions-multiply-divide"):
            for depth in ("intro", "working", "deep"):
                ex = self.example(section_id, depth)
                with self.subTest(section=section_id, depth=depth):
                    a, b, op, c, d = re.fullmatch(r"(\d+)/(\d+) ([+−×÷]) (\d+)/(\d+)", ex["problem"]).groups()
                    expected = ops[op](Fraction(int(a), int(b)), Fraction(int(c), int(d)))
                    self.assertEqual(Fraction(ex["answer"]), expected)
                    self.assertEqual(ex["answer"], str(expected), "answer must be in lowest terms")

    def test_prime_identification_examples(self):
        for depth in ("intro", "working", "deep"):
            ex = self.example("primes-basics", depth)
            n = int(re.fullmatch(r"Is (\d+) prime\?", ex["problem"])[1])
            if is_prime(n):
                self.assertEqual(ex["answer"], f"{n} is prime")
            else:
                self.assertTrue(ex["answer"].startswith(f"{n} is not prime"))
                factors = [int(f) for f in re.findall(r"\d+", ex["answer"])[1:]]
                self.assertEqual(math.prod(factors), n)
        # Remainders quoted in the 223 walk-through.
        self.assertEqual([223 - 217, 223 - 220, 223 - 221], [6, 3, 2])
        self.assertEqual((14 * 14, 15 * 15), (196, 225))
        self.assertEqual([7 * 31, 11 * 20, 13 * 17], [217, 220, 221])

    def test_prime_factorization_examples(self):
        for depth in ("intro", "working", "deep"):
            ex = self.example("primes-factorization", depth)
            n = int(re.search(r"\d+", ex["problem"])[0])
            factorization = parse_factorization(ex["answer"].split(",")[0])
            with self.subTest(n=n):
                self.assertTrue(all(is_prime(p) for p in factorization))
                self.assertEqual(math.prod(p**e for p, e in factorization.items()), n)
                if "factors" in ex["answer"]:
                    divisors = math.prod(e + 1 for e in factorization.values())
                    self.assertEqual(divisors, sum(1 for d in range(1, n + 1) if n % d == 0))
                    self.assertEqual(int(re.search(r"(\d+) factors", ex["answer"])[1]), divisors)

    def test_gcd_lcm_examples(self):
        ex = self.example("primes-gcd-lcm", "intro")
        a, b = map(int, re.findall(r"\d+", ex["problem"]))
        self.assertEqual(ex["answer"], str(math.gcd(a, b)))
        for depth in ("working", "deep"):
            ex = self.example("primes-gcd-lcm", depth)
            a, b = map(int, re.findall(r"\d+", ex["problem"]))
            g, m = re.fullmatch(r"GCF (\d+), LCM (\d+)", ex["answer"]).groups()
            self.assertEqual((int(g), int(m)), (math.gcd(a, b), math.lcm(a, b)))
            self.assertEqual(int(g) * int(m), a * b)

    def test_equation_examples(self):
        for section_id in ("eq-one-step", "eq-two-step", "eq-both-sides"):
            for depth in ("intro", "working", "deep"):
                ex = self.example(section_id, depth)
                with self.subTest(section=section_id, depth=depth):
                    lhs, rhs = re.fullmatch(r"Solve (.+) = (.+)", ex["problem"]).groups()
                    value = to_fraction(re.fullmatch(r"x = (−?\d+)", ex["answer"])[1])
                    self.assertEqual(evaluate_side(lhs, value), evaluate_side(rhs, value))
                    # Linear with a unique solution: any other x must fail.
                    self.assertNotEqual(evaluate_side(lhs, value + 1), evaluate_side(rhs, value + 1))


class ModesTests(unittest.TestCase):
    def test_modes_content(self):
        modes = {m["id"]: m for m in raw_modes()["modes"]}
        self.assertEqual(set(modes), {"guided_path", "map_then_zoom", "mastery_gate"})
        self.assertEqual(modes["guided_path"]["quiz_policy"]["kind"], "section")
        self.assertEqual(modes["mastery_gate"]["quiz_policy"]["kind"], "mastery")
        self.assertIn("80", " ".join(modes["mastery_gate"]["tutor_behaviour"]))
        for mode in modes.values():
            self.assertIn(mode["default_depth"], ("intro", "working", "deep"))
            self.assertIn(mode["quiz_policy"]["kind"], ("diagnostic", "section", "mastery"))
            self.assertTrue(3 <= len(mode["tutor_behaviour"]) <= 5)

    def test_invalid_modes_are_rejected(self):
        for path, value in [
            (("modes", 0, "quiz_policy", "kind"), "exam"),
            (("modes", 0, "default_depth"), "expert"),
            (("modes", 0, "quiz_policy", "when"), "never"),
            (("modes", 0, "id"), "Bad Id"),
            (("modes", 0, "tutor_behaviour"), ["only", "two"]),
        ]:
            with self.subTest(path=path):
                data = raw_modes()
                target = data
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(CurriculumError):
                    validate_modes(data)

    def test_duplicate_mode_ids_rejected(self):
        data = raw_modes()
        data["modes"].append(copy.deepcopy(data["modes"][0]))
        with self.assertRaises(CurriculumError):
            validate_modes(data)


class ValidationRejectsBadDataTests(unittest.TestCase):
    def setUp(self):
        self.topics = copy.deepcopy(raw_topics())

    def mutate_and_expect_error(self, topic_id, mutate, message_part):
        mutate(self.topics[topic_id])
        with self.assertRaises(CurriculumError) as ctx:
            build_catalogue(list(self.topics.values()), raw_modes())
        self.assertIn(message_part, str(ctx.exception))

    def test_missing_depth_block(self):
        self.mutate_and_expect_error("fractions", lambda t: t["sections"][0]["content"].pop("deep"), "deep")

    def test_missing_required_key(self):
        self.mutate_and_expect_error(
            "fractions", lambda t: t["sections"][0]["content"]["intro"].pop("check_questions"), "check_questions")

    def test_bad_ids(self):
        self.mutate_and_expect_error("fractions", lambda t: t.update(id="Fractions"), "does not match")
        self.mutate_and_expect_error("primes", lambda t: t["sections"][0].update(id="a" * 42), "does not match")
        self.mutate_and_expect_error(
            "primes", lambda t: t["sections"][0]["objectives"][0].update(id="Prime.Identify"), "does not match")

    def test_duplicate_objective_id(self):
        def duplicate(t):
            t["sections"][0]["objectives"][0]["id"] = "frac.simplify"
        self.mutate_and_expect_error("fractions", duplicate, "not globally unique")

    def test_cross_topic_duplicate_objective_id(self):
        self.topics["primes"]["sections"][0]["objectives"][0]["id"] = "frac.simplify"
        with self.assertRaises(CurriculumError):
            build_catalogue(list(self.topics.values()), raw_modes())

    def test_unknown_prerequisites(self):
        self.mutate_and_expect_error("fractions", lambda t: t["sections"][1].update(prerequisites=["nope"]), "not a section")
        self.topics = copy.deepcopy(raw_topics())
        self.mutate_and_expect_error("fractions", lambda t: t.update(prerequisites=["nope"]), "not a known topic")

    def test_section_prerequisite_cycle(self):
        def cycle(t):
            t["sections"][0]["prerequisites"] = ["fractions-add-subtract"]
        self.mutate_and_expect_error("fractions", cycle, "cycle")

    def test_topic_prerequisite_cycle(self):
        self.topics["fractions"]["prerequisites"] = ["primes"]
        self.topics["primes"]["prerequisites"] = ["fractions"]
        with self.assertRaises(CurriculumError) as ctx:
            build_catalogue(list(self.topics.values()), raw_modes())
        self.assertIn("cycle", str(ctx.exception))

    def test_size_limits(self):
        def long_explanation(t):
            t["sections"][0]["content"]["intro"]["explanation"] = " ".join(["word"] * 121)
        self.mutate_and_expect_error("fractions", long_explanation, "words")
        self.topics = copy.deepcopy(raw_topics())
        self.mutate_and_expect_error(
            "fractions", lambda t: t["sections"][0]["content"]["intro"].update(key_points=["a", "b"]), "key_points")
        self.topics = copy.deepcopy(raw_topics())
        self.mutate_and_expect_error("fractions", lambda t: t["sections"][0].update(estimated_minutes=31), "estimated_minutes")
        self.topics = copy.deepcopy(raw_topics())
        self.mutate_and_expect_error("fractions", lambda t: t["sections"][0].update(estimated_minutes=4), "estimated_minutes")

    def test_check_questions_must_be_questions(self):
        def statement(t):
            t["sections"][0]["content"]["intro"]["check_questions"] = ["Tell me.", "Why?"]
        self.mutate_and_expect_error("fractions", statement, "question")

    def test_unexpected_keys_rejected(self):
        self.mutate_and_expect_error("fractions", lambda t: t.update(extra=1), "unexpected keys")

    def test_validate_topic_accepts_real_data(self):
        for topic in raw_topics().values():
            validate_topic(topic)

    def test_load_fails_fast_on_missing_directory(self):
        with self.assertRaises(CurriculumError):
            load_curriculum(Path("this-directory-does-not-exist"))

    def test_depth_constant(self):
        self.assertEqual(catalogue.DEPTHS, ("intro", "working", "deep"))


if __name__ == "__main__":
    unittest.main()
