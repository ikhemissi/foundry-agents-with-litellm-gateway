"""Question model and answer matching for the quiz agent (pure functions, no I/O)."""

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from typing import Any

MAX_ANSWER_CHARS = 64
CHOICE_LETTERS = "ABCD"

_TRANSLATION = str.maketrans(
    {
        "\u2212": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",  # minus / dashes
        "\u00d7": "*", "\u00b7": "*", "\u22c5": "*", "\u2217": "*",  # multiplication signs
        "\u00f7": "/", "\u2044": "/", "\u2215": "/",  # division signs and fraction slashes
    }
)
_INT = re.compile(r"[+-]?\d{1,18}")
_FRACTION = re.compile(r"([+-]?\d{1,18})(?:/([+-]?\d{1,18}))?")
_POWER = re.compile(r"(\d{1,7})(?:\^(\d{1,2}))?")
_LETTER = re.compile(r"\(?([a-d])[).:]?")
_LETTER_AND_TEXT = re.compile(r"\(?([a-d])[).:](.+)")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:/\d+)?(?!\d)")


def normalise(text: str) -> str:
    """Trim, lowercase and map unicode minus/times/divide signs to ASCII."""
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATION).strip().lower()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalise(text))


def numeric_tokens(text: str) -> list[str]:
    """Integers and a/b fractions in `text`, in order (a leading '-' only when attached to the number)."""
    return _NUMBER.findall(text.translate(_TRANSLATION))


def parse_int(text: str) -> int | None:
    """Plain integers only: thousands separators ("1,000") and decimals are not understood."""
    compact = _compact(text)
    return int(compact) if _INT.fullmatch(compact) else None


def parse_fraction(text: str) -> tuple[int, int] | None:
    match = _FRACTION.fullmatch(_compact(text))
    if not match:
        return None
    numerator, denominator = int(match[1]), int(match[2] or 1)
    return (numerator, denominator) if denominator != 0 else None


def parse_int_list(text: str) -> list[int] | None:
    """'2, 2, 3', '2*2*3', '2 × 3' or '2^2*3' -> sorted factors; None if not understood."""
    tokens = [token for token in re.split(r"[,*\s]+", normalise(text)) if token]
    if not 0 < len(tokens) <= 24:
        return None
    values: list[int] = []
    for token in tokens:
        match = _POWER.fullmatch(token)
        if not match:
            return None
        values.extend([int(match[1])] * int(match[2] or 1))
    return sorted(values) if len(values) <= 24 else None


@dataclass(frozen=True)
class Question:
    id: str
    objective: str
    type: str  # fraction | integer | integer_list | choice
    prompt: str
    answer_format: str
    match: str  # int | int_var:<name> | frac_value | frac_lowest | frac_exact | int_list | choice
    expected: Any  # int | Fraction | (num, den) | list[int] | choice index
    display: str  # human-readable expected answer, only revealed after a wrong answer
    explanation: str
    choices: tuple[str, ...] | None = None
    facts: tuple[str, ...] = ()  # strings a rewritten prompt must keep verbatim


def is_correct(question: Question, raw: str) -> bool:
    match question.match.partition(":"):
        case ("int", _, _):
            return parse_int(raw) == question.expected
        case ("int_var", _, variable):
            compact = _compact(raw)
            if compact.startswith(f"{variable}="):
                compact = compact[len(variable) + 1 :]
            return bool(_INT.fullmatch(compact)) and int(compact) == question.expected
        case ("frac_value", _, _):
            parsed = parse_fraction(raw)
            return parsed is not None and Fraction(*parsed) == question.expected
        case ("frac_lowest", _, _):
            parsed = parse_fraction(raw)
            if parsed is None or parsed[1] < 1 or gcd(abs(parsed[0]), parsed[1]) != 1:
                return False
            return Fraction(*parsed) == question.expected
        case ("frac_exact", _, _):
            return parse_fraction(raw) == question.expected
        case ("int_list", _, _):
            return parse_int_list(raw) == sorted(question.expected)
        case ("choice", _, _):
            return _choice_index(question, raw) == question.expected
    raise ValueError(f"unknown match strategy {question.match!r}")


def _choice_index(question: Question, raw: str) -> int | None:
    assert question.choices is not None
    letter = _LETTER.fullmatch(_compact(raw))
    if letter:
        index = CHOICE_LETTERS.index(letter[1].upper())
        if index < len(question.choices):
            return index
    compact = _compact(raw)
    for index, choice in enumerate(question.choices):
        if _compact(choice) == compact:
            return index
    # The form shown to learners, "C) 3/12": the letter and the text must agree.
    combined = _LETTER_AND_TEXT.fullmatch(compact)
    if combined:
        index = CHOICE_LETTERS.index(combined[1].upper())
        if index < len(question.choices) and _compact(question.choices[index]) == combined[2]:
            return index
    return None
