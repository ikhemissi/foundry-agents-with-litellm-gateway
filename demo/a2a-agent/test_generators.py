"""Property tests: every generated question's answer is recomputed independently from the prompt text.

The oracle below parses each prompt and solves it with fractions.Fraction, math.gcd/lcm, brute-force
divisor search and substitution into the equation, none of which share code with generators.py.
"""

import re
import unittest
from fractions import Fraction
from math import gcd, lcm

from curriculum_index import CURRICULUM, topic_objectives
from generators import LEVELS, TEMPLATES, build_quiz, select_objectives
from questions import is_correct

SEEDS = range(60)
INT = r"(-?\d+)"


def divisors(n):
    return [d for d in range(1, n + 1) if n % d == 0]


def prime_bf(n):
    return n >= 2 and divisors(n) == [1, n]


def factor_bf(n):
    """Prime factors by repeatedly splitting off the smallest divisor above 1 (no sieve, no square-root bound)."""
    factors = []
    while n > 1:
        smallest = next(d for d in range(2, n + 1) if n % d == 0)
        factors.append(smallest)
        n //= smallest
    return factors


def fractions_expression(text):
    tokens = text.split()
    value = Fraction(*map(int, tokens[0].split("/")))
    for operator, operand in zip(tokens[1::2], tokens[2::2]):
        other = Fraction(*map(int, operand.split("/")))
        value = {"+": value + other, "-": value - other, "\u00d7": value * other, "\u00f7": value / other}[operator]
    return value


def linear(expression, value):
    python = re.sub(r"(\d)([a-z(])", r"\1*\2", expression.replace("\u2212", "-"))
    return eval(python, {"__builtins__": {}}, {"x": Fraction(value), "y": Fraction(value), "n": Fraction(value)})  # noqa: S307


def sides_equal(equation, value):
    left, right = equation.split(" = ")
    return linear(left, value) == linear(right, value)


def roots(equation):
    """Exact root of a linear equation by evaluating both sides at two points, then confirming by substitution."""
    left, right = equation.split(" = ")

    def difference(value):
        return linear(left, value) - linear(right, value)

    slope = difference(1) - difference(0)
    if slope == 0:
        return []
    root = -difference(0) / slope
    assert difference(root) == 0
    return [int(root)] if root.denominator == 1 else []


def choice_indices(q, predicate):
    return {i for i, text in enumerate(q.choices) if predicate(text)}


INVERSE = {"multiply by": "Divide by", "divide by": "Multiply by", "add": "Subtract", "subtract": "Add"}
FIRST_STEP = [
    (r"^[xyn] \+ (\d+) = \d+$", "Subtract {} from both sides"),
    (r"^[xyn] - (\d+) = \d+$", "Add {} to both sides"),
    (r"^\d+[xyn] = \d+$", None),
    (r"^[xyn]/(\d+) = \d+$", "Multiply both sides by {}"),
]


def oracle(q):
    """Returns ('value', v) or ('choices', set-of-correct-indices) computed independently of the generator."""
    p = q.prompt
    m = lambda pattern: re.search(pattern, p)  # noqa: E731
    if x := m(r"missing numerator: (\d+)/(\d+) = \?/(\d+)"):
        return "value", int(Fraction(int(x[1]), int(x[2])) * int(x[3]))
    if x := m(r"Which fraction is equivalent to (\d+)/(\d+)\?"):
        target = Fraction(int(x[1]), int(x[2]))
        return "choices", choice_indices(q, lambda t: Fraction(*map(int, t.split("/"))) == target)
    if x := m(r"missing denominator: (\d+)/(\d+) = (\d+)/\?"):
        return "value", int(int(x[3]) / Fraction(int(x[1]), int(x[2])))
    if x := m(r"equivalent to (\d+)/(\d+) whose denominator is (\d+)"):
        return "value", (int(Fraction(int(x[1]), int(x[2])) * int(x[3])), int(x[3]))
    if x := m(r"Simplify (\d+)/(\d+) to lowest terms"):
        return "value", Fraction(int(x[1]), int(x[2]))
    if m(r"NOT in lowest terms"):
        return "choices", choice_indices(q, lambda t: gcd(*map(int, t.split("/"))) > 1)
    if x := m(r"(\d+) out of (\d+) students"):
        return "value", Fraction(int(x[1]), int(x[2]))
    if x := m(r"least common denominator of (\d+)/(\d+) and (\d+)/(\d+)"):
        return "value", lcm(int(x[2]), int(x[4]))
    if x := m(r"Rewrite (\d+)/(\d+) and (\d+)/(\d+) using their least common denominator"):
        return "value", int(x[1]) * lcm(int(x[2]), int(x[4])) // int(x[2])
    if x := m(r"common denominator of (\d+)/(\d+) and (\d+)/(\d+)"):
        return "choices", choice_indices(q, lambda t: int(t) % int(x[2]) == 0 and int(t) % int(x[4]) == 0)
    if x := m(r"^Calculate (.+)\. Give your answer as a fraction\.$"):
        return "value", fractions_expression(x[1])
    if x := m(r"Sam drank (\d+)/(\d+) of a bottle .* and (\d+)/(\d+) of a bottle"):
        return "value", Fraction(int(x[1]), int(x[2])) + Fraction(int(x[3]), int(x[4]))
    if x := m(r"What is (\d+)/(\d+) of (\d+)\?"):
        return "value", Fraction(int(x[1]), int(x[2])) * int(x[3])
    if x := m(r"uses (\d+)/(\d+) of a cup .* only (\d+)/(\d+) of the recipe"):
        return "value", Fraction(int(x[1]), int(x[2])) * Fraction(int(x[3]), int(x[4]))
    if x := m(r"Dividing by (\d+)/(\d+) is the same as multiplying"):
        return "value", 1 / Fraction(int(x[1]), int(x[2]))
    if x := m(r"ribbon (\d+) m long .* each (\d+)/(\d+) m long"):
        return "value", int(Fraction(int(x[1])) / Fraction(int(x[2]), int(x[3])))
    if m(r"Which of these numbers is prime\?"):
        return "choices", choice_indices(q, lambda t: prime_bf(int(t)))
    if m(r"Which of these numbers is composite\?"):
        return "choices", choice_indices(q, lambda t: int(t) > 1 and not prime_bf(int(t)))
    if x := m(r"Is (\d+) a prime number\?"):
        return "choices", {0 if prime_bf(int(x[1])) else 1}
    if x := m(r"How many prime numbers are there from (\d+) to (\d+)"):
        return "value", sum(prime_bf(n) for n in range(int(x[1]), int(x[2]) + 1))
    if x := m(r"Which of these numbers is divisible by (\d+)\?"):
        return "choices", choice_indices(q, lambda t: int(t) % int(x[1]) == 0)
    if x := m(r"smallest digit that can replace the \? so that (\S+) is divisible by (\d+)"):
        masked, divisor = x[1], int(x[2])
        return "value", next(d for d in range(10) if int(masked.replace("?", str(d))) % divisor == 0)
    if x := m(r"How many multiples of (\d+) are there from 1 to (\d+)"):
        return "value", sum(1 for k in range(1, int(x[2]) + 1) if k % int(x[1]) == 0)
    if x := m(r"Is (\d+) divisible by (\d+)\?"):
        return "choices", {0 if int(x[1]) % int(x[2]) == 0 else 1}
    if x := m(r"factor tree for (\d+), the first split"):
        return "value", int(x[1]) // factor_bf(int(x[1]))[0]
    if x := m(r"How many prime leaves does the complete factor tree of (\d+)"):
        return "value", len(factor_bf(int(x[1])))
    if x := m(r"prime leaf .* in the factor tree of (\d+)\?"):
        n = int(x[1])
        return "choices", choice_indices(q, lambda t: prime_bf(int(t)) and n % int(t) == 0)
    if x := m(r"Write (\d+) as a product of prime factors"):
        return "value", factor_bf(int(x[1]))
    if x := m(r"largest prime factor of (\d+)"):
        return "value", max(factor_bf(int(x[1])))
    if x := m(r"How many different prime numbers divide (\d+)"):
        return "value", len(set(factor_bf(int(x[1]))))
    if x := m(r"Write (\d+) as a product of primes and add up"):
        return "value", sum(factor_bf(int(x[1])))
    if x := m(r"GCD\) of (\d+), (\d+) and (\d+)"):
        return "value", gcd(gcd(int(x[1]), int(x[2])), int(x[3]))
    if x := m(r"GCD\) of (\d+) and (\d+)"):
        return "value", max(d for d in divisors(int(x[1])) if int(x[2]) % d == 0)
    if x := m(r"ribbons are (\d+) cm and (\d+) cm"):
        return "value", gcd(int(x[1]), int(x[2]))
    if m(r"pair of numbers is coprime"):
        return "choices", choice_indices(q, lambda t: gcd(*map(int, t.split(" and "))) == 1)
    if x := m(r"Which of these is the least common multiple \(LCM\) of (\d+) and (\d+)"):
        a, b = int(x[1]), int(x[2])
        smallest = next(k for k in range(1, a * b + 1) if k % a == 0 and k % b == 0)
        return "choices", choice_indices(q, lambda t: int(t) == smallest)
    if x := m(r"\(LCM\) of (\d+), (\d+) and (\d+)"):
        nums = [int(v) for v in x.groups()]
        return "value", next(k for k in range(1, nums[0] * nums[1] * nums[2] + 1) if all(k % n == 0 for n in nums))
    if x := m(r"\(LCM\) of (\d+) and (\d+)"):
        a, b = int(x[1]), int(x[2])
        return "value", next(k for k in range(1, a * b + 1) if k % a == 0 and k % b == 0)
    if x := m(r"every (\d+) minutes and another every (\d+) minutes"):
        a, b = int(x[1]), int(x[2])
        return "value", next(k for k in range(1, a * b + 1) if k % a == 0 and k % b == 0)
    if x := m(r"^Solve for ([xyn]): (.+)$"):
        found = roots(x[2])
        assert len(found) == 1, (p, found)
        return "value", found[0]
    if x := m(r"Which operation undoes '(.+) (\d+)'\?"):
        wanted = f"{INVERSE[x[1]]} {x[2]}"
        return "choices", choice_indices(q, lambda t: t == wanted)
    if x := m(r"To solve (.+), what should you do to both sides first\?"):
        equation = x[1]
        for pattern, template in FIRST_STEP:
            hit = re.match(pattern, equation)
            if hit:
                wanted = template.format(hit[1]) if template else f"Divide both sides by {re.match(r'(\d+)', equation)[1]}"
                return "choices", choice_indices(q, lambda t: t == wanted)
        raise AssertionError(equation)
    if x := m(r"multiplied by (\d+) and then (\d+) is added, giving (-?\d+)"):
        return "value", (int(x[3]) - int(x[2])) // int(x[1])
    if x := m(r"(\d+) is added to a number and the result is multiplied by (\d+), giving (-?\d+)"):
        return "value", int(x[3]) // int(x[2]) - int(x[1])
    if x := m(r"add (\d+)\. The result is (\d+)"):
        return "value", int(x[2]) - int(x[1])
    if x := m(r"decreased by (\d+) is (-?\d+)"):
        return "value", int(x[2]) + int(x[1])
    if x := m(r"^(\d+) times a number is (\d+)"):
        return "value", int(x[2]) // int(x[1])
    if x := m(r"Which value of x is the solution of (.+)\?"):
        return "choices", choice_indices(q, lambda t: sides_equal(x[1], int(t)))
    if x := m(r"Is x = (-?\d+) a solution of (.+)\?"):
        return "choices", {0 if sides_equal(x[2], int(x[1])) else 1}
    if x := m(r"Substitute x = (-?\d+) into (.+)\. What value"):
        return "value", linear(x[2], int(x[1]))
    raise AssertionError(f"no oracle for prompt: {p!r}")


def check(test, q):
    kind, result = oracle(q)
    if kind == "choices":
        test.assertEqual(result, {q.expected}, (q.prompt, q.choices, q.expected))
        test.assertEqual(len(set(q.choices)), len(q.choices), q.choices)
        return
    expected = q.expected
    if q.match == "frac_exact":
        test.assertEqual(result, expected, q.prompt)
    elif q.type == "integer_list":
        test.assertEqual(sorted(result), sorted(expected), q.prompt)
    else:
        test.assertEqual(Fraction(result), Fraction(expected), q.prompt)


class GeneratorTests(unittest.TestCase):
    def test_every_template_is_independently_verified_over_many_seeds_and_levels(self):
        seen_templates = set()
        for kind, topic, section in [("section", t, s) for t, secs in CURRICULUM.items() for s in secs]:
            for level in LEVELS:
                for seed in SEEDS:
                    for q in build_quiz(kind, topic, section, level, 10, seed):
                        with self.subTest(topic=topic, section=section, level=level, seed=seed, q=q.id):
                            check(self, q)
                        seen_templates.add(q.objective)
        self.assertEqual(seen_templates, {o for objectives in map(topic_objectives, CURRICULUM) for o in objectives})

    def test_every_template_function_is_exercised_and_correct(self):
        # Direct template calls guarantee that templates the quiz builder might not pick are also checked.
        import random

        for objective, templates in TEMPLATES.items():
            for template in templates:
                for level in LEVELS:
                    for seed in range(120):
                        q = template(random.Random(seed), level)
                        with self.subTest(template=template.__name__, level=level, seed=seed):
                            self.assertEqual(q.objective, objective)
                            check(self, q)

    def test_every_objective_has_at_least_two_distinct_templates(self):
        all_objectives = {o for topic in CURRICULUM for o in topic_objectives(topic)}
        self.assertEqual(set(TEMPLATES), all_objectives)
        for objective, templates in TEMPLATES.items():
            self.assertGreaterEqual(len(templates), 2, objective)
            import random

            shapes = {re.sub(r"\d+", "#", t(random.Random(1), "working").prompt) for t in templates}
            self.assertEqual(len(shapes), len(templates), objective)

    def test_canonical_answers_are_accepted_and_wrong_ones_rejected(self):
        for topic, sections in CURRICULUM.items():
            for section in sections:
                for level in LEVELS:
                    for seed in range(30):
                        for q in build_quiz("section", topic, section, level, 10, seed):
                            answer = q.display
                            if q.type == "choice":
                                answer = "ABCD"[q.expected]
                            elif q.match.startswith("int_var"):
                                answer = answer.split("=")[1]
                            self.assertTrue(is_correct(q, answer), (q.prompt, answer))
                            self.assertFalse(is_correct(q, "definitely wrong"), q.prompt)
                            self.assertFalse(is_correct(q, ""), q.prompt)

    def test_same_inputs_give_identical_quizzes_and_seeds_differ(self):
        args = ("section", "primes", "primes-gcd-lcm", "working", 8)
        self.assertEqual(build_quiz(*args, 7), build_quiz(*args, 7))
        self.assertNotEqual([q.prompt for q in build_quiz(*args, 7)], [q.prompt for q in build_quiz(*args, 8)])

    def test_questions_in_a_quiz_are_distinct_and_ids_sequential(self):
        for topic, sections in CURRICULUM.items():
            for section in sections:
                for level in LEVELS:
                    for seed in range(40):
                        quiz = build_quiz("mastery", topic, section, level, 10, seed)
                        self.assertEqual([q.id for q in quiz], [f"q{i}" for i in range(1, 11)])
                        keys = {(q.prompt, q.choices) for q in quiz}
                        self.assertEqual(len(keys), 10, (topic, section, level, seed))

    def test_objective_selection(self):
        fractions = topic_objectives("fractions")
        self.assertEqual(
            select_objectives("section", "fractions", "fractions-add-subtract", 5),
            ["frac.common-denominator", "frac.add-sub"] * 2 + ["frac.common-denominator"],
        )
        self.assertEqual(select_objectives("diagnostic", "fractions", None, 6), fractions)
        for count in range(3, 11):
            picked = select_objectives("diagnostic", "primes", None, count)
            self.assertEqual(len(picked), count)
            if count <= 6:
                self.assertEqual(len(set(picked)), count)
                self.assertEqual(picked, sorted(picked, key=topic_objectives("primes").index))
                self.assertEqual(picked[0], topic_objectives("primes")[0])
                self.assertEqual(picked[-1], topic_objectives("primes")[-1])
        self.assertEqual(select_objectives("diagnostic", "primes", None, 5), select_objectives("diagnostic", "primes", None, 5))

    def test_levels_scale_difficulty(self):
        def largest(level):
            numbers = []
            for seed in range(40):
                for q in build_quiz("section", "primes", "primes-gcd-lcm", level, 6, seed):
                    numbers += [int(n) for n in re.findall(r"\d+", q.prompt)]
            return max(numbers)

        self.assertLess(largest("intro"), largest("working"))
        self.assertLess(largest("working"), largest("deep"))


if __name__ == "__main__":
    unittest.main()
