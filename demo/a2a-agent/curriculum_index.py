"""Curriculum scope of the quiz agent: topic -> section -> learning objectives (in teaching order).

Mirrors the ids served by the MCP curriculum server; a repo-level test compares the two.
"""

CURRICULUM: dict[str, dict[str, list[str]]] = {
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


def topic_objectives(topic_id: str) -> list[str]:
    """All objectives of a topic in curriculum order."""
    return [objective for objectives in CURRICULUM[topic_id].values() for objective in objectives]
