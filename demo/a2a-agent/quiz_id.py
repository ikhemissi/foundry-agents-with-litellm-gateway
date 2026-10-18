"""Short, tamper-evident, stateless quiz ids that a language model can copy exactly.

An id is 24 Crockford-base32 characters (15 bytes) shown in six groups, for example `k3x9-0mq2-7fd8-hr5w-c2n4-ybzp`:

    payload (10 bytes) = flags(2) | topic index(1) | section index(1) | seed(6)
    tag     (5 bytes)  = HMAC-SHA256(key, curriculum fingerprint + payload)

The curriculum fingerprint is part of the tag input rather than the payload, so reordering or editing the
curriculum makes older ids invalid instead of silently selecting a different quiz. Topic and section indices
follow curriculum order, so the curriculum must stay append-only. Ids contain no answers, but the seed is
readable by anyone who also has the source code: the signature prevents tampering, not reading.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass

from curriculum_index import CURRICULUM

KINDS = ("diagnostic", "section", "mastery")
LEVELS = ("intro", "working", "deep")
VERSION = 1
MIN_KEY_CHARS = 32
MAX_INPUT_CHARS = 128
MAX_SEED = 2**48
NO_SECTION = 0xFF
ID_CHARS = 24
GROUP = 4

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford: no i, l, o or u
_VALUE = {char: index for index, char in enumerate(_ALPHABET)}
_ALIASES = {"o": "0", "i": "1", "l": "1"}  # visually ambiguous characters decode to the intended digit
_NOISE = re.compile(r"[\s\-_]+")
_SURROUNDING = " \t\r\n\"'`“”‘’<>()[]{}.,;:"


class InvalidQuizId(ValueError):
    pass


@dataclass(frozen=True)
class QuizSpec:
    kind: str
    topic: str
    section: str | None
    level: str
    count: int
    seed: int


def _encode(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    return "".join(_ALPHABET[(bits >> shift) & 31] for shift in range(len(data) * 8 - 5, -1, -5))


def _decode(text: str) -> bytes:
    bits = 0
    for char in text:
        bits = (bits << 5) | _VALUE[char]
    return bits.to_bytes(len(text) * 5 // 8, "big")


def _fingerprint() -> bytes:
    """Hash of the ordered curriculum, so a reordered or edited curriculum invalidates older ids."""
    detailed = "|".join(f"{t}/{s}:{','.join(o)}" for t, sections in CURRICULUM.items() for s, o in sections.items())
    return hashlib.sha256(f"{VERSION}\n{detailed}".encode()).digest()[:8]


def normalise(quiz_id: object) -> str:
    """Case, whitespace, hyphens and surrounding punctuation are ignored; anything else must be exact."""
    if not isinstance(quiz_id, str) or not 0 < len(quiz_id) <= MAX_INPUT_CHARS:
        raise InvalidQuizId
    text = _NOISE.sub("", quiz_id.strip(_SURROUNDING)).lower()
    text = "".join(_ALIASES.get(char, char) for char in text)
    if len(text) != ID_CHARS or any(char not in _VALUE for char in text):
        raise InvalidQuizId
    return text


class QuizIdCodec:
    def __init__(self, key: str) -> None:
        if not isinstance(key, str) or len(key) < MIN_KEY_CHARS:
            raise ValueError(f"QUIZ_SIGNING_KEY is required and must be at least {MIN_KEY_CHARS} characters long.")
        self._key = key.encode("utf-8")
        self._fingerprint = _fingerprint()

    def _tag(self, payload: bytes) -> bytes:
        return hmac.new(self._key, self._fingerprint + payload, hashlib.sha256).digest()[:5]

    def encode(self, spec: QuizSpec) -> str:
        topics = list(CURRICULUM)
        topic = topics.index(spec.topic)
        section = NO_SECTION if spec.section is None else list(CURRICULUM[spec.topic]).index(spec.section)
        flags = (VERSION << 13) | (KINDS.index(spec.kind) << 11) | (LEVELS.index(spec.level) << 9) | ((spec.count - 3) << 5)
        payload = flags.to_bytes(2, "big") + bytes([topic, section]) + spec.seed.to_bytes(6, "big")
        text = _encode(payload + self._tag(payload))
        return "-".join(text[i : i + GROUP] for i in range(0, ID_CHARS, GROUP))

    def decode(self, quiz_id: object) -> QuizSpec:
        raw = _decode(normalise(quiz_id))
        payload, tag = raw[:10], raw[10:]
        # Compare the raw bytes in constant time; the strict alphabet and fixed length mean one encoding per id.
        if not hmac.compare_digest(self._tag(payload), tag):
            raise InvalidQuizId
        flags = int.from_bytes(payload[:2], "big")
        version, kind, level, count, spare = flags >> 13, (flags >> 11) & 3, (flags >> 9) & 3, (flags >> 5) & 15, flags & 31
        topics = list(CURRICULUM)
        if version != VERSION or spare or kind >= len(KINDS) or level >= len(LEVELS) or count > 7:
            raise InvalidQuizId
        if payload[2] >= len(topics):
            raise InvalidQuizId
        topic = topics[payload[2]]
        sections = list(CURRICULUM[topic])
        if KINDS[kind] == "diagnostic":
            if payload[3] != NO_SECTION:
                raise InvalidQuizId
            section = None
        elif payload[3] < len(sections):
            section = sections[payload[3]]
        else:
            raise InvalidQuizId
        return QuizSpec(KINDS[kind], topic, section, LEVELS[level], count + 3, int.from_bytes(payload[4:10], "big"))
