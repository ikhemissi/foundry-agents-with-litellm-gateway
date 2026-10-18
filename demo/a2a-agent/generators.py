"""Deterministic question templates and quiz construction.

Every template is a pure function of (random.Random, level); the quiz builder derives each question's
generator from the quiz seed only, so grading can regenerate exactly the same quiz.
"""

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from math import gcd, lcm

from curriculum_index import CURRICULUM, topic_objectives
from questions import CHOICE_LETTERS, Question, numeric_tokens

LEVELS = ("intro", "working", "deep")
Template = Callable[[random.Random, str], Question]
SMALL_PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]
CHOICE_FORMAT = "a letter A-D (or the text of the choice)"
FRACTION_FORMAT = "a fraction written as numerator/denominator (a whole number if the result is whole)"
LOWEST_FORMAT = "a fraction written as numerator/denominator, in lowest terms"
INTEGER_FORMAT = "a whole number"


def tier(level: str, intro, working, deep):
    return (intro, working, deep)[LEVELS.index(level)]


def draft(objective, qtype, prompt, answer_format, match, expected, display, explanation, choices=None, facts=None):
    tokens = tuple(numeric_tokens(prompt)) if facts is None else tuple(facts)
    return Question("", objective, qtype, prompt, answer_format, match, expected, display, explanation, choices, tokens)


def choice(rng, objective, prompt, correct, distractors, explanation, facts=None):
    """Four-option multiple choice: the correct answer plus three distractors, shuffled."""
    pool = list(dict.fromkeys(d for d in distractors if d != correct))
    options = rng.sample(pool, 3) + [correct]
    rng.shuffle(options)
    index = options.index(correct)
    return draft(
        objective, "choice", prompt, CHOICE_FORMAT, "choice", index, f"{CHOICE_LETTERS[index]}) {correct}",
        explanation, tuple(options), facts,
    )


def yes_no(objective, prompt, is_yes, explanation, facts=None):
    index = 0 if is_yes else 1
    return draft(
        objective, "choice", prompt, "A or B (or Yes / No)", "choice", index, f"{CHOICE_LETTERS[index]}) {('Yes', 'No')[index]}",
        explanation, ("Yes", "No"), facts,
    )


def integer(objective, prompt, value, explanation, facts=None, match="int", fmt=INTEGER_FORMAT, display=None):
    return draft(objective, "integer", prompt, fmt, match, value, display or str(value), explanation, None, facts)


def fraction_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def fraction_answer(objective, prompt, value: Fraction, explanation, match="frac_value", fmt=FRACTION_FORMAT, facts=None):
    return draft(objective, "fraction", prompt, fmt, match, value, fraction_text(value), explanation, None, facts)


# ---------------------------------------------------------------------------------------------- fractions

def proper(rng, lo, hi):
    b = rng.randint(max(lo, 2), hi)
    return rng.randint(1, b - 1), b


def coprime_proper(rng, lo, hi):
    while True:
        a, b = proper(rng, lo, hi)
        if gcd(a, b) == 1:
            return a, b


def denominators(rng, level, count):
    """`count` denominators: equal at intro, otherwise varied."""
    if level == "intro":
        return [rng.randint(2, 8)] * count
    top = tier(level, 8, 10, 15)
    return [rng.randint(2, top) for _ in range(count)]


def operands(rng, level, count):
    return [(rng.randint(1, d - 1), d) for d in denominators(rng, level, count)]


def frac_equivalent_missing_numerator(rng, level):
    a, b = proper(rng, 2, tier(level, 6, 10, 15))
    k = rng.randint(2, tier(level, 3, 6, 9))
    return integer(
        "frac.equivalent", f"Fill in the missing numerator: {a}/{b} = ?/{b * k}", a * k,
        f"{b} x {k} = {b * k}, so multiply the numerator by {k} too: {a} x {k} = {a * k}.",
    )


def frac_equivalent_choice(rng, level):
    a, b = proper(rng, 2, tier(level, 6, 10, 15))
    k = rng.randint(2, tier(level, 3, 6, 9))
    correct = f"{a * k}/{b * k}"
    distractors = [f"{a * k}/{b * k + rng.randint(1, 2)}", f"{a + k}/{b + k}", f"{a * k + 1}/{b * k}", f"{b * k}/{a * k}"]
    return choice(
        rng, "frac.equivalent", f"Which fraction is equivalent to {a}/{b}?", correct, distractors,
        f"Multiplying top and bottom of {a}/{b} by {k} gives {correct}.",
    )


def frac_equivalent_missing_denominator(rng, level):
    a, b = proper(rng, 2, tier(level, 6, 10, 15))
    k = rng.randint(2, tier(level, 3, 6, 9))
    return integer(
        "frac.equivalent", f"Fill in the missing denominator: {a}/{b} = {a * k}/?", b * k,
        f"{a} x {k} = {a * k}, so multiply the denominator by {k} too: {b} x {k} = {b * k}.",
    )


def frac_equivalent_write(rng, level):
    a, b = proper(rng, 2, tier(level, 6, 10, 15))
    k = rng.randint(2, tier(level, 3, 6, 9))
    return draft(
        "frac.equivalent", "fraction", f"Write a fraction equivalent to {a}/{b} whose denominator is {b * k}.",
        f"a fraction with denominator {b * k}", "frac_exact", (a * k, b * k), f"{a * k}/{b * k}",
        f"Multiply top and bottom by {k}: {a * k}/{b * k}.",
    )


def frac_simplify_direct(rng, level):
    a, b = coprime_proper(rng, 2, tier(level, 6, 10, 15))
    g = rng.randint(2, tier(level, 4, 9, 12))
    return fraction_answer(
        "frac.simplify", f"Simplify {a * g}/{b * g} to lowest terms.", Fraction(a, b),
        f"Both {a * g} and {b * g} are divisible by {g}: {a * g}/{b * g} = {a}/{b}.", match="frac_lowest", fmt=LOWEST_FORMAT,
    )


def frac_simplify_choice(rng, level):
    a, b = coprime_proper(rng, 2, tier(level, 6, 10, 15))
    g = rng.randint(2, tier(level, 4, 9, 12))
    correct = f"{a * g}/{b * g}"
    distractors = []
    while len(distractors) < 6:
        x, y = coprime_proper(rng, 3, tier(level, 12, 20, 40))
        if f"{x}/{y}" not in distractors:
            distractors.append(f"{x}/{y}")
    return choice(
        rng, "frac.simplify", "Which of these fractions is NOT in lowest terms?", correct, distractors,
        f"{a * g} and {b * g} share the factor {g}, so {correct} can be simplified to {a}/{b}.",
    )


def frac_simplify_word(rng, level):
    a, b = coprime_proper(rng, 2, tier(level, 6, 10, 15))
    g = rng.randint(2, tier(level, 4, 9, 12))
    return fraction_answer(
        "frac.simplify",
        f"{a * g} out of {b * g} students walk to school. What fraction of the students walk? Give it in lowest terms.",
        Fraction(a, b), f"{a * g}/{b * g}, divided top and bottom by {g}, is {a}/{b}.", match="frac_lowest", fmt=LOWEST_FORMAT,
    )


def distinct_denominators(rng, level):
    top = tier(level, 6, 10, 15)
    b = rng.randint(2, top)
    d = rng.choice([x for x in range(2, top + 1) if x != b])
    return (rng.randint(1, b - 1), b), (rng.randint(1, d - 1), d)


def frac_cd_lcd(rng, level):
    (a, b), (c, d) = distinct_denominators(rng, level)
    return integer(
        "frac.common-denominator", f"What is the least common denominator of {a}/{b} and {c}/{d}?", lcm(b, d),
        f"The least common multiple of {b} and {d} is {lcm(b, d)}.",
    )


def frac_cd_numerator(rng, level):
    (a, b), (c, d) = distinct_denominators(rng, level)
    common = lcm(b, d)
    return integer(
        "frac.common-denominator",
        f"Rewrite {a}/{b} and {c}/{d} using their least common denominator. What is the new numerator of {a}/{b}?",
        a * common // b, f"The least common denominator is {common}. {a}/{b} = {a * common // b}/{common}.",
    )


def frac_cd_choice(rng, level):
    (a, b), (c, d) = distinct_denominators(rng, level)
    common = lcm(b, d)
    correct = str(common * rng.choice([1, 2]))
    candidates = {common + 1, common - 1, b + d, common + b, common + d, common * 2 + 1, max(b, d) + 1}
    wrong = [str(n) for n in sorted(candidates) if n > 1 and (n % b or n % d)]
    return choice(
        rng, "frac.common-denominator", f"Which of these is a common denominator of {a}/{b} and {c}/{d}?", correct, wrong,
        f"A common denominator must be divisible by both {b} and {d}; {correct} is.",
    )


def _result_text(parts: list[tuple[int, int]], signs: list[str]) -> str:
    text = f"{parts[0][0]}/{parts[0][1]}"
    for sign, (n, d) in zip(signs, parts[1:]):
        text += f" {sign} {n}/{d}"
    return text


def frac_add(rng, level):
    parts = operands(rng, level, 2)
    value = sum(Fraction(n, d) for n, d in parts)
    expression = _result_text(parts, ["+"])
    return fraction_answer(
        "frac.add-sub", f"Calculate {expression}. Give your answer as a fraction.", value,
        f"Use a common denominator: {expression} = {fraction_text(value)}.", facts=(expression,),
    )


def frac_sub(rng, level):
    parts = sorted(operands(rng, level, 2), key=lambda p: Fraction(*p), reverse=True)
    value = Fraction(*parts[0]) - Fraction(*parts[1])
    if value == 0:
        return frac_sub(rng, level)
    expression = _result_text(parts, ["-"])
    return fraction_answer(
        "frac.add-sub", f"Calculate {expression}. Give your answer as a fraction.", value,
        f"Use a common denominator: {expression} = {fraction_text(value)}.", facts=(expression,),
    )


def frac_add_word(rng, level):
    (a, b), (c, d) = operands(rng, level, 2)
    value = Fraction(a, b) + Fraction(c, d)
    return fraction_answer(
        "frac.add-sub",
        f"Sam drank {a}/{b} of a bottle in the morning and {c}/{d} of a bottle in the afternoon. "
        "How much of a bottle did Sam drink in total?",
        value, f"{a}/{b} + {c}/{d} = {fraction_text(value)} of a bottle.",
    )


def frac_three_terms(rng, level):
    while True:
        parts = operands(rng, level, 3)
        value = Fraction(*parts[0]) + Fraction(*parts[1]) - Fraction(*parts[2])
        if value > 0:
            break
    expression = _result_text(parts, ["+", "-"])
    return fraction_answer(
        "frac.add-sub", f"Calculate {expression}. Give your answer as a fraction.", value,
        f"Work left to right with a common denominator: {expression} = {fraction_text(value)}.", facts=(expression,),
    )


def frac_multiply(rng, level):
    (a, b), (c, d) = operands(rng, level, 2) if level != "intro" else [proper(rng, 2, 6), proper(rng, 2, 6)]
    value = Fraction(a, b) * Fraction(c, d)
    expression = f"{a}/{b} \u00d7 {c}/{d}"
    return fraction_answer(
        "frac.multiply", f"Calculate {expression}. Give your answer as a fraction.", value,
        f"Multiply tops and bottoms: {a * c}/{b * d} = {fraction_text(value)}.", facts=(expression,),
    )


def frac_multiply_of_whole(rng, level):
    a, b = proper(rng, 2, tier(level, 6, 10, 12))
    k = rng.randint(2, tier(level, 6, 9, 15))
    return integer(
        "frac.multiply", f"What is {a}/{b} of {b * k}?", a * k,
        f"{b * k} / {b} = {k}, then {k} x {a} = {a * k}.",
    )


def frac_multiply_word(rng, level):
    (a, b), (c, d) = [proper(rng, 2, tier(level, 6, 10, 12)) for _ in range(2)]
    value = Fraction(a, b) * Fraction(c, d)
    return fraction_answer(
        "frac.multiply",
        f"A recipe uses {a}/{b} of a cup of sugar. Mia makes only {c}/{d} of the recipe. How many cups of sugar does she use?",
        value, f"{a}/{b} \u00d7 {c}/{d} = {fraction_text(value)} cup.",
    )


def frac_multiply_three(rng, level):
    parts = [proper(rng, 2, tier(level, 5, 8, 10)) for _ in range(3)]
    value = Fraction(1)
    for n, d in parts:
        value *= Fraction(n, d)
    expression = " \u00d7 ".join(f"{n}/{d}" for n, d in parts)
    return fraction_answer(
        "frac.multiply", f"Calculate {expression}. Give your answer as a fraction.", value,
        f"Multiply all the tops and all the bottoms, then simplify: {fraction_text(value)}.", facts=(expression,),
    )


def frac_divide_direct(rng, level):
    (a, b), (c, d) = [proper(rng, 2, tier(level, 6, 10, 12)) for _ in range(2)]
    value = Fraction(a, b) / Fraction(c, d)
    expression = f"{a}/{b} \u00f7 {c}/{d}"
    return fraction_answer(
        "frac.divide", f"Calculate {expression}. Give your answer as a fraction.", value,
        f"Flip the second fraction and multiply: {a}/{b} \u00d7 {d}/{c} = {fraction_text(value)}.", facts=(expression,),
    )


def frac_divide_reciprocal(rng, level):
    while True:
        d = rng.randint(3, tier(level, 7, 11, 15))
        c = rng.randint(2, d - 1)
        if gcd(c, d) == 1:
            break
    return fraction_answer(
        "frac.divide", f"Dividing by {c}/{d} is the same as multiplying by which fraction?", Fraction(d, c),
        f"Dividing by a fraction means multiplying by its reciprocal: {d}/{c}.",
    )


def frac_divide_word(rng, level):
    c, d = proper(rng, 2, tier(level, 5, 8, 12))
    m = rng.randint(1, tier(level, 3, 5, 8))
    return integer(
        "frac.divide",
        f"A ribbon {c * m} m long is cut into pieces that are each {c}/{d} m long. How many pieces are there?", d * m,
        f"{c * m} \u00f7 {c}/{d} = {c * m} \u00d7 {d}/{c} = {d * m}.",
    )


# ---------------------------------------------------------------------------------------------- primes

def is_prime(n: int) -> bool:
    return n >= 2 and all(n % f for f in range(2, int(n**0.5) + 1))


def factorize(n: int) -> list[int]:
    factors, f = [], 2
    while f * f <= n:
        while n % f == 0:
            factors.append(f)
            n //= f
        f += 1
    return factors + ([n] if n > 1 else [])


def random_prime(rng, hi):
    return rng.choice([n for n in range(2, hi + 1) if is_prime(n)])


def random_composites(rng, hi, count):
    pool = [n for n in range(4, hi + 1) if not is_prime(n)]
    return rng.sample(pool, count)


def composite(rng, level):
    """A composite number with at least two prime factors, size by level."""
    hi = tier(level, 60, 200, 1000)
    primes = [p for p in SMALL_PRIMES if p <= tier(level, 7, 13, 31)]
    most = tier(level, 3, 4, 5)
    while True:
        n = 1
        for _ in range(rng.randint(2, most)):
            n *= rng.choice(primes)
        if 4 <= n <= hi:
            return n


def prime_which_prime(rng, level):
    hi = tier(level, 30, 100, 300)
    correct = random_prime(rng, hi)
    return choice(
        rng, "prime.identify", "Which of these numbers is prime?", str(correct), [str(n) for n in random_composites(rng, hi, 5)],
        f"{correct} has no divisors other than 1 and itself; the other numbers each have another factor.",
    )


def prime_which_composite(rng, level):
    hi = tier(level, 30, 100, 300)
    correct = random_composites(rng, hi, 1)[0]
    primes = rng.sample([n for n in range(2, hi + 1) if is_prime(n)], 4)
    smallest = factorize(correct)[0]
    return choice(
        rng, "prime.identify", "Which of these numbers is composite?", str(correct), [str(n) for n in primes],
        f"{correct} is divisible by {smallest}, so it is composite; the others are prime.",
    )


def prime_yes_no(rng, level):
    hi = tier(level, 40, 120, 400)
    n = random_prime(rng, hi) if rng.random() < 0.5 else random_composites(rng, hi, 1)[0]
    explanation = f"{n} is prime." if is_prime(n) else f"{n} is divisible by {factorize(n)[0]}, so it is not prime."
    return yes_no("prime.identify", f"Is {n} a prime number?", is_prime(n), explanation)


def prime_count_in_range(rng, level):
    low = rng.randint(1, tier(level, 10, 60, 200))
    high = low + rng.randint(*tier(level, (10, 20), (30, 60), (50, 100)))
    primes = [n for n in range(low, high + 1) if is_prime(n)]
    return integer(
        "prime.identify", f"How many prime numbers are there from {low} to {high} (including both ends)?", len(primes),
        f"The primes are {', '.join(map(str, primes))}: {len(primes)} of them.",
    )


def divisibility_which(rng, level):
    d = rng.choice(tier(level, (2, 3, 5, 10), (2, 3, 4, 5, 6, 9, 10), (3, 4, 6, 8, 9, 11)))
    lo, hi = tier(level, (10, 99), (100, 999), (1000, 9999))
    correct = d * rng.randint(-(-lo // d), hi // d)
    wrong = []
    while len(wrong) < 5:
        n = rng.randint(lo, hi)
        if n % d and n not in wrong:
            wrong.append(n)
    return choice(
        rng, "prime.divisibility", f"Which of these numbers is divisible by {d}?", str(correct), [str(n) for n in wrong],
        f"{correct} = {d} x {correct // d}, so it is divisible by {d}; the others leave a remainder.",
    )


def divisibility_missing_digit(rng, level):
    length = tier(level, 3, 4, 5)
    m = rng.choice((3, 9))
    digits = [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(length - 1)]
    position = rng.randint(1, length - 1)
    masked = "".join("?" if i == position else str(x) for i, x in enumerate(digits))
    rest = sum(x for i, x in enumerate(digits) if i != position)
    answer = next(x for x in range(10) if (rest + x) % m == 0)
    return integer(
        "prime.divisibility", f"Find the smallest digit that can replace the ? so that {masked} is divisible by {m}.", answer,
        f"A number is divisible by {m} when its digit sum is. The other digits add up to {rest}; the smallest digit that makes the total a multiple of {m} is {answer}.",
        facts=(masked, str(m)),
    )


def divisibility_count_multiples(rng, level):
    m = rng.randint(*tier(level, (2, 5), (3, 12), (6, 25)))
    n = rng.randint(*tier(level, (30, 60), (60, 200), (200, 1000)))
    return integer(
        "prime.divisibility", f"How many multiples of {m} are there from 1 to {n} (including {n} if it is one)?", n // m,
        f"{n} \u00f7 {m} = {n / m:.2f}..., so the whole number of multiples is {n // m}.",
    )


def divisibility_yes_no(rng, level):
    m = rng.choice(tier(level, (2, 3, 5, 10), (3, 4, 6, 9, 11), (6, 7, 8, 9, 11, 12)))
    lo, hi = tier(level, (10, 99), (100, 999), (1000, 9999))
    n = m * rng.randint(-(-lo // m), hi // m) if rng.random() < 0.5 else rng.randint(lo, hi)
    explanation = f"{n} = {m} x {n // m}." if n % m == 0 else f"{n} divided by {m} leaves remainder {n % m}."
    return yes_no("prime.divisibility", f"Is {n} divisible by {m}?", n % m == 0, explanation)


def tree_first_split(rng, level):
    n = composite(rng, level)
    p = factorize(n)[0]
    return integer(
        "prime.factor-tree",
        f"In a factor tree for {n}, the first split is {n} = p \u00d7 m, where p is the smallest prime factor of {n}. What is m?",
        n // p, f"The smallest prime factor of {n} is {p}, so m = {n} \u00f7 {p} = {n // p}.", facts=(str(n),),
    )


def tree_leaf_count(rng, level):
    n = composite(rng, level)
    factors = factorize(n)
    return integer(
        "prime.factor-tree",
        f"How many prime leaves does the complete factor tree of {n} have? (Count a repeated prime every time it appears.)",
        len(factors), f"{n} = {' x '.join(map(str, factors))}, which is {len(factors)} prime leaves.",
    )


def tree_which_leaf(rng, level):
    n = composite(rng, level)
    factors = set(factorize(n))
    correct = rng.choice(sorted(factors))
    others = [p for p in SMALL_PRIMES[:10] if p not in factors]
    distractors = [str(n), "1", *map(str, others[:4])]
    divisors = [x for x in range(4, n) if n % x == 0 and not is_prime(x)]
    distractors += map(str, divisors[:3])
    return choice(
        rng, "prime.factor-tree", f"Which of these could be a prime leaf (a number that cannot be split further) in the factor tree of {n}?",
        str(correct), distractors,
        f"{correct} is a prime that divides {n}; {n} itself and the other composite divisors can still be split, and the remaining numbers do not divide {n}.",
    )


def factorization_list(rng, level):
    n = composite(rng, level)
    factors = factorize(n)
    return draft(
        "prime.prime-factorization",
        "integer_list",
        f"Write {n} as a product of prime factors. List the primes separated by commas, repeating a prime as often as it occurs.",
        "primes separated by commas, repeating a prime as often as it occurs", "int_list", factors, ", ".join(map(str, factors)),
        f"Divide by primes: {n} = {' x '.join(map(str, factors))}.", None, (str(n),),
    )


def factorization_largest(rng, level):
    n = composite(rng, level)
    factors = factorize(n)
    return integer(
        "prime.prime-factorization", f"What is the largest prime factor of {n}?", factors[-1],
        f"{n} = {' x '.join(map(str, factors))}; the largest prime there is {factors[-1]}.",
    )


def factorization_distinct(rng, level):
    n = composite(rng, level)
    factors = factorize(n)
    return integer(
        "prime.prime-factorization", f"How many different prime numbers divide {n}?", len(set(factors)),
        f"{n} = {' x '.join(map(str, factors))}, which uses {len(set(factors))} different primes.",
    )


def factorization_sum(rng, level):
    n = composite(rng, level)
    factors = factorize(n)
    return integer(
        "prime.prime-factorization", f"Write {n} as a product of primes and add up all the prime factors (counting repeats). What is the sum?",
        sum(factors), f"{n} = {' x '.join(map(str, factors))}, and {' + '.join(map(str, factors))} = {sum(factors)}.",
    )


def coprime_pair(rng, hi):
    while True:
        x, y = rng.randint(1, hi), rng.randint(1, hi)
        if x != y and gcd(x, y) == 1:
            return x, y


def gcd_direct(rng, level):
    g = rng.randint(*tier(level, (2, 6), (2, 12), (3, 20)))
    x, y = coprime_pair(rng, tier(level, 6, 10, 15))
    return integer(
        "prime.gcd", f"What is the greatest common divisor (GCD) of {g * x} and {g * y}?", g,
        f"{g * x} = {g} x {x} and {g * y} = {g} x {y}, and {x} and {y} share no factor, so the GCD is {g}.",
    )


def gcd_word(rng, level):
    g = rng.randint(*tier(level, (2, 6), (3, 12), (4, 20)))
    x, y = coprime_pair(rng, tier(level, 6, 10, 15))
    return integer(
        "prime.gcd",
        f"Two ribbons are {g * x} cm and {g * y} cm long. They are cut into equal pieces, as long as possible, with nothing left over. How long is each piece in cm?",
        g, f"The longest piece that fits both ribbons is their GCD: {g}.",
    )


def gcd_coprime_pair(rng, level):
    hi = tier(level, 20, 60, 150)
    while True:
        x, y = rng.randint(2, hi), rng.randint(2, hi)
        if x != y and gcd(x, y) == 1:
            break
    wrong = set()
    while len(wrong) < 5:
        p, q = rng.randint(2, hi), rng.randint(2, hi)
        if p != q and gcd(p, q) > 1:
            wrong.add(f"{p} and {q}")
    return choice(
        rng, "prime.gcd", "Which pair of numbers is coprime (their only common divisor is 1)?", f"{x} and {y}", sorted(wrong),
        f"{x} and {y} have GCD 1; each other pair shares a factor greater than 1.",
    )


def gcd_three(rng, level):
    while True:
        g = rng.randint(*tier(level, (2, 5), (2, 8), (3, 12)))
        xs = [rng.randint(1, tier(level, 6, 9, 12)) for _ in range(3)]
        if len(set(xs)) == 3 and gcd(gcd(xs[0], xs[1]), xs[2]) == 1:
            break
    a, b, c = (g * x for x in xs)
    return integer(
        "prime.gcd", f"What is the greatest common divisor (GCD) of {a}, {b} and {c}?", g,
        f"The numbers are {g} times {xs[0]}, {xs[1]} and {xs[2]}, which share nothing more, so the GCD is {g}.",
    )


def lcm_pair(rng, level):
    lo, hi = tier(level, (2, 9), (3, 15), (6, 30))
    a, b = rng.sample(range(lo, hi + 1), 2)
    return integer(
        "prime.lcm", f"What is the least common multiple (LCM) of {a} and {b}?", lcm(a, b),
        f"Multiples of {max(a, b)} until one is also a multiple of {min(a, b)}: the LCM is {lcm(a, b)}.",
    )


def lcm_word(rng, level):
    lo, hi = tier(level, (2, 8), (3, 12), (6, 25))
    a, b = rng.sample(range(lo, hi + 1), 2)
    return integer(
        "prime.lcm",
        f"One bus leaves every {a} minutes and another every {b} minutes. They leave together now. After how many minutes will they next leave together?",
        lcm(a, b), f"They meet again at the least common multiple of {a} and {b}: {lcm(a, b)} minutes.",
    )


def lcm_three(rng, level):
    lo, hi = tier(level, (2, 6), (2, 10), (3, 15))
    a, b, c = rng.sample(range(lo, hi + 1), 3)
    return integer(
        "prime.lcm", f"What is the least common multiple (LCM) of {a}, {b} and {c}?", lcm(a, b, c),
        f"Find the LCM of {a} and {b}, then of that and {c}: {lcm(a, b, c)}.",
    )


def lcm_choice(rng, level):
    lo, hi = tier(level, (2, 9), (3, 15), (6, 30))
    a, b = rng.sample(range(lo, hi + 1), 2)
    value = lcm(a, b)
    pool = {a * b, gcd(a, b), 2 * value, value + a, value + b, max(a, b)}
    return choice(
        rng, "prime.lcm", f"Which of these is the least common multiple (LCM) of {a} and {b}?", str(value),
        [str(n) for n in sorted(pool) if n != value],
        f"{value} is the smallest number that both {a} and {b} divide into.",
    )


# ---------------------------------------------------------------------------------------------- equations

def term(coef, var):
    return var if coef == 1 else f"-{var}" if coef == -1 else f"{coef}{var}"


def lin(a, b, var="x"):
    return term(a, var) + (f" + {b}" if b > 0 else f" - {-b}" if b < 0 else "")


def const_first(d, c, var="x"):
    return f"{d} + {term(c, var)}" if c > 0 else f"{d} - {term(-c, var)}"


def solution(rng, level):
    if level == "intro":
        return rng.randint(1, 9)
    return rng.choice([n for n in range(*tier(level, (0, 0), (-9, 13), (-15, 21))) if n != 0])


def constant(rng, level):
    top = tier(level, 9, 25, 60)
    return rng.randint(1, top) if level == "intro" else rng.choice([n for n in range(-top, top + 1) if n != 0])


def equation_answer(objective, v, equation, x0, explanation, prompt=None):
    return integer(
        objective, prompt or f"Solve for {v}: {equation}", x0, explanation, facts=(equation,),
        match=f"int_var:{v}", fmt=f"a whole number (writing {v} = ... is also fine)", display=f"{v} = {x0}",
    )


def eq_one_step_additive(rng, level):
    v, a, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 9, 25, 60)), solution(rng, level)
    variant = rng.choice(("plus", "minus", "swap"))
    if variant == "plus":
        equation, how = f"{v} + {a} = {x0 + a}", f"Subtract {a} from both sides"
    elif variant == "minus":
        equation, how = f"{v} - {a} = {x0 - a}", f"Add {a} to both sides"
    else:
        equation, how = f"{a} + {v} = {x0 + a}", f"Subtract {a} from both sides"
    return equation_answer("eq.one-step", v, equation, x0, f"{how}: {v} = {x0}.")


def eq_one_step_multiplicative(rng, level):
    v, a, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 15)), solution(rng, level)
    if rng.random() < 0.5:
        return equation_answer("eq.one-step", v, f"{a}{v} = {a * x0}", x0, f"Divide both sides by {a}: {v} = {x0}.")
    return equation_answer(
        "eq.one-step", v, f"{v}/{a} = {x0}", a * x0, f"Multiply both sides by {a}: {v} = {a * x0}.",
    )


def eq_one_step_word(rng, level):
    a, x0 = rng.randint(2, tier(level, 9, 20, 40)), rng.randint(1, tier(level, 9, 20, 40))
    kind = rng.choice(("add", "sub", "mul"))
    if kind == "add":
        prompt, how = f"I think of a number and add {a}. The result is {x0 + a}. What is my number?", f"Subtract {a}: {x0}."
    elif kind == "sub":
        prompt, how = f"A number decreased by {a} is {x0 - a}. What is the number?", f"Add {a} back: {x0}."
    else:
        prompt, how = f"{a} times a number is {a * x0}. What is the number?", f"Divide by {a}: {x0}."
    return integer("eq.one-step", prompt, x0, how)


OPERATIONS = (("multiply by", "Divide by"), ("divide by", "Multiply by"), ("add", "Subtract"), ("subtract", "Add"))


def eq_inverse_operation(rng, level):
    a = rng.randint(2, tier(level, 9, 25, 99))
    operation, inverse = rng.choice(OPERATIONS)
    options = [f"{name} {a}" for _, name in OPERATIONS]
    return choice(
        rng, "eq.inverse-ops", f"Which operation undoes '{operation} {a}'?", f"{inverse} {a}", options,
        f"{inverse} {a} is the inverse of '{operation} {a}'.",
    )


def eq_inverse_first_step(rng, level):
    v, a, c = rng.choice("xyn"), rng.randint(2, tier(level, 9, 20, 40)), rng.randint(10, tier(level, 30, 90, 200))
    equation, correct = rng.choice(
        [(f"{v} + {a} = {c}", f"Subtract {a} from both sides"), (f"{v} - {a} = {c}", f"Add {a} to both sides"),
         (f"{a}{v} = {c}", f"Divide both sides by {a}"), (f"{v}/{a} = {c}", f"Multiply both sides by {a}")]
    )
    options = [f"Subtract {a} from both sides", f"Add {a} to both sides", f"Divide both sides by {a}", f"Multiply both sides by {a}"]
    return choice(
        rng, "eq.inverse-ops", f"To solve {equation}, what should you do to both sides first?", correct, options,
        f"{correct} to undo the operation that is applied to {v}.", facts=(equation,),
    )


def eq_inverse_backwards(rng, level):
    m, c, x0 = rng.randint(2, tier(level, 5, 9, 12)), rng.randint(1, tier(level, 9, 20, 40)), solution(rng, level)
    if rng.random() < 0.5:
        prompt = f"A number is multiplied by {m} and then {c} is added, giving {m * x0 + c}. Working backwards with inverse operations, what was the number?"
        how = f"Subtract {c}: {m * x0}; divide by {m}: {x0}."
    else:
        prompt = f"{c} is added to a number and the result is multiplied by {m}, giving {m * (x0 + c)}. Working backwards, what was the number?"
        how = f"Divide by {m}: {x0 + c}; subtract {c}: {x0}."
    return integer("eq.inverse-ops", prompt, x0, how)


def eq_two_step_basic(rng, level):
    v, a, b, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), constant(rng, level), solution(rng, level)
    equation = f"{lin(a, b, v)} = {a * x0 + b}"
    return equation_answer("eq.two-step", v, equation, x0, f"Move the {b:+d} across, then divide by {a}: {a * x0} / {a} = {x0}.")


def eq_two_step_fraction(rng, level):
    v, a, b, k = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), constant(rng, level), solution(rng, level)
    equation = f"{v}/{a} {'+' if b > 0 else '-'} {abs(b)} = {k + b}"
    return equation_answer("eq.two-step", v, equation, a * k, f"Undo the {b:+d}: {v}/{a} = {k}; multiply by {a}: {v} = {a * k}.")


def eq_two_step_brackets(rng, level):
    v, a, b, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), constant(rng, level), solution(rng, level)
    equation = f"{a}({v} {'+' if b > 0 else '-'} {abs(b)}) = {a * (x0 + b)}"
    return equation_answer("eq.two-step", v, equation, x0, f"Divide by {a}: {v} {b:+d} = {x0 + b}; then undo the {b:+d}: {v} = {x0}.")


def eq_negative_only(rng, level):
    v, a, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), solution(rng, level)
    return equation_answer("eq.negative-coeff", v, f"-{a}{v} = {-a * x0}", x0, f"Divide both sides by -{a}: {v} = {x0}.")


def eq_negative_subtract(rng, level):
    v, a, b, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), rng.randint(1, tier(level, 9, 25, 60)), solution(rng, level)
    equation = f"{b} - {a}{v} = {b - a * x0}"
    return equation_answer("eq.negative-coeff", v, equation, x0, f"Subtract {b}: -{a}{v} = {-a * x0}; divide by -{a}: {v} = {x0}.")


def eq_negative_with_constant(rng, level):
    v, a, b, x0 = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), constant(rng, level), solution(rng, level)
    equation = f"{lin(-a, b, v)} = {-a * x0 + b}"
    return equation_answer("eq.negative-coeff", v, equation, x0, f"Undo the constant: -{a}{v} = {-a * x0}; divide by -{a}: {v} = {x0}.")


def eq_negative_fraction(rng, level):
    v, a, k = rng.choice("xyn"), rng.randint(2, tier(level, 5, 9, 12)), rng.randint(1, tier(level, 9, 12, 15))
    return equation_answer(
        "eq.negative-coeff", v, f"-{v}/{a} = {-k}", a * k, f"Multiply both sides by -{a}: {v} = {a * k}.",
    )


def both_sides_coefficients(rng, level):
    if level == "intro":
        a = rng.randint(2, 6)
        return a, rng.randint(1, a - 1)
    pool = [n for n in range(*tier(level, (1, 1), (1, 10), (-9, 10))) if n != 0]
    a = rng.choice(pool)
    return a, rng.choice([n for n in pool if n != a])


def eq_both_sides_basic(rng, level):
    v, x0 = rng.choice("xyn"), solution(rng, level)
    a, c = both_sides_coefficients(rng, level)
    b = rng.randint(1, tier(level, 9, 25, 60)) if level == "intro" else constant(rng, level)
    d = (a - c) * x0 + b
    equation = f"{lin(a, b, v)} = {lin(c, d, v)}"
    return equation_answer("eq.variables-both-sides", v, equation, x0, f"Subtract {term(c, v)} from both sides, then undo the constants: {(a - c)}{v} = {(a - c) * x0}, so {v} = {x0}.")


def eq_both_sides_constant_first(rng, level):
    v, x0 = rng.choice("xyn"), solution(rng, level)
    a, c = both_sides_coefficients(rng, level)
    b = rng.randint(1, tier(level, 9, 25, 60)) if level == "intro" else constant(rng, level)
    d = (a - c) * x0 + b
    equation = f"{lin(a, b, v)} = {const_first(d, c, v)}"
    return equation_answer("eq.variables-both-sides", v, equation, x0, f"Collect the {v} terms on one side: {(a - c)}{v} = {(a - c) * x0}, so {v} = {x0}.")


def eq_both_sides_brackets(rng, level):
    v, x0 = rng.choice("xyn"), solution(rng, level)
    a = rng.randint(2, tier(level, 5, 8, 9))
    c = rng.choice([n for n in range(1, tier(level, 5, 9, 10)) if n != a])
    b = rng.randint(1, tier(level, 5, 9, 15))
    d = a * (x0 + b) - c * x0
    equation = f"{a}({v} + {b}) = {lin(c, d, v)}"
    return equation_answer("eq.variables-both-sides", v, equation, x0, f"Expand to {a}{v} + {a * b} = {lin(c, d, v)}, collect the {v} terms, and solve: {v} = {x0}.")


def eq_check_which_value(rng, level):
    v, a, b, x0 = "x", rng.randint(2, tier(level, 5, 9, 12)), constant(rng, level), solution(rng, level)
    equation = f"{lin(a, b, v)} = {a * x0 + b}"
    options = {x0 + 1, x0 - 1, x0 + 2, -x0, x0 * 2, x0 - 2} - {x0}
    return choice(
        rng, "eq.check-solution", f"Which value of x is the solution of {equation}?", str(x0), [str(n) for n in sorted(options)],
        f"Substituting x = {x0}: {a * x0 + b} = {a * x0 + b}. The other values do not make both sides equal.", facts=(equation,),
    )


def eq_check_yes_no(rng, level):
    a, b, x0 = rng.randint(2, tier(level, 5, 9, 12)), constant(rng, level), solution(rng, level)
    equation = f"{lin(a, b)} = {a * x0 + b}"
    k = x0 if rng.random() < 0.5 else x0 + rng.choice([-2, -1, 1, 2])
    return yes_no(
        "eq.check-solution", f"Is x = {k} a solution of {equation}?", k == x0,
        f"Substituting x = {k} gives {a * k + b}, and the right side is {a * x0 + b}: {'equal' if k == x0 else 'not equal'}.",
        facts=(f"x = {k}", equation),
    )


def eq_check_substitute(rng, level):
    a, b, k = rng.randint(2, tier(level, 5, 9, 12)), rng.randint(1, tier(level, 9, 20, 40)), solution(rng, level)
    if level == "deep":
        c = rng.randint(1, 9)
        expression, value = f"{a}x + {b} - {c}x", a * k + b - c * k
    else:
        expression, value = lin(a, b), a * k + b
    return integer(
        "eq.check-solution", f"Substitute x = {k} into {expression}. What value do you get?", value,
        f"Replace x by {k}: {expression.replace('x', f'({k})')} = {value}.", facts=(f"x = {k}", expression),
    )


TEMPLATES: dict[str, list[Template]] = {
    "frac.equivalent": [frac_equivalent_missing_numerator, frac_equivalent_choice, frac_equivalent_missing_denominator, frac_equivalent_write],
    "frac.simplify": [frac_simplify_direct, frac_simplify_choice, frac_simplify_word],
    "frac.common-denominator": [frac_cd_lcd, frac_cd_numerator, frac_cd_choice],
    "frac.add-sub": [frac_add, frac_sub, frac_add_word, frac_three_terms],
    "frac.multiply": [frac_multiply, frac_multiply_of_whole, frac_multiply_word, frac_multiply_three],
    "frac.divide": [frac_divide_direct, frac_divide_reciprocal, frac_divide_word],
    "prime.identify": [prime_which_prime, prime_yes_no, prime_count_in_range, prime_which_composite],
    "prime.divisibility": [divisibility_which, divisibility_missing_digit, divisibility_count_multiples, divisibility_yes_no],
    "prime.factor-tree": [tree_first_split, tree_leaf_count, tree_which_leaf],
    "prime.prime-factorization": [factorization_list, factorization_largest, factorization_distinct, factorization_sum],
    "prime.gcd": [gcd_direct, gcd_word, gcd_coprime_pair, gcd_three],
    "prime.lcm": [lcm_pair, lcm_word, lcm_three, lcm_choice],
    "eq.one-step": [eq_one_step_additive, eq_one_step_multiplicative, eq_one_step_word],
    "eq.inverse-ops": [eq_inverse_operation, eq_inverse_first_step, eq_inverse_backwards],
    "eq.two-step": [eq_two_step_basic, eq_two_step_fraction, eq_two_step_brackets],
    "eq.negative-coeff": [eq_negative_only, eq_negative_subtract, eq_negative_with_constant, eq_negative_fraction],
    "eq.variables-both-sides": [eq_both_sides_basic, eq_both_sides_constant_first, eq_both_sides_brackets],
    "eq.check-solution": [eq_check_which_value, eq_check_yes_no, eq_check_substitute],
}


def select_objectives(kind: str, topic: str, section: str | None, count: int) -> list[str]:
    """Objective of each question slot: diagnostics spread over the whole topic, others round-robin a section."""
    if kind != "diagnostic":
        objectives = CURRICULUM[topic][section]
        return [objectives[i % len(objectives)] for i in range(count)]
    objectives = topic_objectives(topic)
    if count >= len(objectives):
        return [objectives[i % len(objectives)] for i in range(count)]
    span = len(objectives) - 1
    return [objectives[(2 * i * span + count - 1) // (2 * (count - 1))] for i in range(count)]


def build_quiz(kind: str, topic: str, section: str | None, level: str, count: int, seed: int) -> list[Question]:
    """The quiz is a pure function of its arguments: same inputs, same questions and answers."""
    slots = select_objectives(kind, topic, section, count)
    used: Counter[str] = Counter()
    seen: set[tuple] = set()
    questions: list[Question] = []
    for index, objective in enumerate(slots):
        templates = TEMPLATES[objective]
        start = random.Random(f"{seed}:rot:{objective}").randrange(len(templates))
        position = start + used[objective]
        used[objective] += 1
        for attempt in range(60):
            template = templates[(position + attempt // 20) % len(templates)]
            candidate = template(random.Random(f"{seed}:{index}:{attempt}"), level)
            key = (candidate.prompt, candidate.choices)
            if key not in seen:
                break
        seen.add(key)
        questions.append(replace(candidate, id=f"q{index + 1}"))
    return questions
