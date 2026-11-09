"""Ticket replay helpers for the console (SPEC §12): flag text that looks like an injection.

The ticket is customer-written and untrusted. The console shows it as plain text only;
this module just splits it into segments and marks the suspicious ones, so a merchant can
see hidden instructions (HTML comments, fake system notices, invisible characters) at a glance.
Flagging is a reading aid, never a security control: Wardlock's guarantees do not depend on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (pattern, why). Order matters only for the label when spans overlap.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL), "hidden HTML comment"),
    (re.compile(r"<\s*/?\s*(?:script|style|iframe|img|svg|object)\b[^>]*>", re.IGNORECASE), "HTML tag"),
    (re.compile(r"\b(?:system|admin(?:istrator)?|developer|assistant|staff|supervisor)\b"
                r"[ \t]*(?:notice|message|note|prompt|override|instruction|instructions)?[ \t]*:", re.IGNORECASE),
     "claims to be from staff or the system"),
    (re.compile(r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|rules?|messages?)", re.IGNORECASE), "tries to override the agent's rules"),
    (re.compile(r"\b(?:policy update|pre-?approved|do not (?:mention|tell)|new instructions?|you are now)\b",
                re.IGNORECASE), "tries to override the agent's rules"),
    (re.compile(r"\b(?=[0-9A-Z]{17}\b)(?=[0-9A-Z]*[0-9])(?=[0-9A-Z]*[A-Z])[0-9A-Z]{17}\b"),
     "looks like a PayPal payment ID"),
]
# Zero-width, bidirectional-override and other invisible characters.
_INVISIBLE = re.compile(r"[­​-‏‪-‮⁠-⁤﻿]")


@dataclass(frozen=True)
class Segment:
    text: str
    suspicious: bool = False
    why: str | None = None


def segments(text: str) -> list[Segment]:
    """Split `text` into plain and suspicious segments. Joined back, they show every character:
    invisible ones are replaced with a visible [U+XXXX] marker."""
    spans: list[tuple[int, int, str]] = []
    for pattern, why in _PATTERNS:
        spans += [(m.start(), m.end(), why) for m in pattern.finditer(text) if m.end() > m.start()]
    spans += [(m.start(), m.end(), "invisible character") for m in _INVISIBLE.finditer(text)]

    merged: list[list] = []
    for start, end, why in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            if why not in merged[-1][2]:
                merged[-1][2].append(why)
        else:
            merged.append([start, end, [why]])

    out: list[Segment] = []
    cursor = 0
    for start, end, whys in merged:
        if start > cursor:
            out.append(Segment(text[cursor:start]))
        out.append(Segment(_visible(text[start:end]), True, "; ".join(whys)))
        cursor = end
    if cursor < len(text):
        out.append(Segment(text[cursor:]))
    return out


def _visible(text: str) -> str:
    return _INVISIBLE.sub(lambda m: f"[U+{ord(m.group()):04X}]", text)
