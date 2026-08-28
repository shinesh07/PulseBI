"""Faithfulness verification: every numeral must resolve to a computed fact.

This is what makes "the model does not produce quantitative truth" a checked
property rather than a claim. After a narrative is rendered -- by the template
provider or by a language model, identically -- every number in the text is
extracted and matched against the evidence package that produced it. A numeral
that resolves to nothing means the narrator invented a figure, and the API
returns an abstention instead of the sentence.

The distinction being tested is *faithfulness*, not *factuality*. Factuality
asks whether a statement matches the world, which this system cannot check.
Faithfulness asks whether a statement matches its own source, which it can check
completely, because it owns the source.

Deliberately not an LLM judge. Judge models exhibit position, verbosity and
self-enhancement bias, and human-human agreement on evaluation benchmarks runs
around 63-66%. Numeral resolution is deterministic and has none of those failure
modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Matches currency, percentages, percentage points, scientific notation and
# plain decimals, with or without thousands separators.
_NUMERAL = re.compile(
    r"""
    (?<![\w/.-])                 # not mid-word, mid-date or mid-path
    (?P<sign>[+-]?)
    \$?
    (?P<body>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?P<exp>[eE][+-]?\d+)?
    \s*(?P<unit>%|pp|percentage\s+points)?
    """,
    re.VERBOSE,
)

# ISO dates and date fragments are identifiers, not quantitative claims.
_DATE = re.compile(r"\d{4}-\d{2}(-\d{2})?")


@dataclass(frozen=True)
class Numeral:
    text: str
    value: float
    unit: str | None
    position: int


@dataclass
class VerificationResult:
    ok: bool
    numerals_found: int
    numerals_resolved: int
    unresolved: list[Numeral] = field(default_factory=list)
    matched: dict[str, str] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        if self.numerals_found == 0:
            return 1.0
        return self.numerals_resolved / self.numerals_found

    def failure_message(self) -> str:
        quoted = ", ".join(repr(n.text) for n in self.unresolved)
        return (
            f"Narrative contains {len(self.unresolved)} numeral(s) that resolve to no "
            f"computed fact: {quoted}. Refusing to publish an unverifiable statement."
        )


def extract_numerals(text: str) -> list[Numeral]:
    """Every quantitative token in the text, excluding dates."""
    masked = _DATE.sub(lambda m: "#" * len(m.group(0)), text)

    numerals: list[Numeral] = []
    for match in _NUMERAL.finditer(masked):
        raw = match.group(0).strip()
        body = match.group("body").replace(",", "")
        exponent = match.group("exp") or ""
        try:
            value = float(f"{match.group('sign')}{body}{exponent}")
        except ValueError:
            continue
        unit = match.group("unit")
        numerals.append(
            Numeral(
                text=raw,
                value=value,
                unit="pp" if unit and "p" in unit else ("%" if unit == "%" else None),
                position=match.start(),
            )
        )
    return numerals


def display_tolerance(text: str, floor: float) -> float:
    """How far a faithfully-rounded rendering may sit from the true value.

    A figure written to one decimal place carries no more information than
    "within half of the last digit": -4.8 is a faithful rendering of anything in
    [-4.85, -4.75]. Checking such a numeral against a flat absolute tolerance
    rejects correct prose, which is how this function came to exist -- the
    template rendered a computed -4.75 as "-4.8%" and verification failed.

    Deriving the tolerance from the numeral's own precision keeps the check
    tight where the narrator was precise and loose only where it was not. A
    fabricated figure still has to land within half a display unit of a real
    computed value, which is a strong constraint, not a loophole.
    """
    body = text.strip().lstrip("+-$").replace(",", "").rstrip("% ").rstrip("pp").strip()
    if "e" in body.lower():
        # Scientific notation: fall back to a relative comparison below.
        return floor
    decimals = len(body.split(".")[1]) if "." in body else 0
    # A value sitting exactly on the rounding boundary -- 128.75 rendered as
    # "128.8" -- differs by exactly half a unit, and in binary floating point
    # that subtraction lands a few ulps above the threshold. The epsilon absorbs
    # representation error without widening the tolerance in any meaningful way.
    return max(floor, 0.5 * (10.0**-decimals) + 1e-9)


def _matches(claimed: float, known: float, tolerance: float) -> bool:
    if abs(claimed - known) <= tolerance:
        return True
    # A narrator may legitimately quote a magnitude and carry the direction in
    # words: "compressed by 3.10 pp" for a value of -3.10.
    if abs(abs(claimed) - abs(known)) <= tolerance:
        return True
    # Large figures and scientific notation are quoted at display precision, so
    # compare relatively rather than absolutely.
    scale = max(abs(claimed), abs(known))
    if scale > 1_000:
        return abs(abs(claimed) - abs(known)) / scale <= 1e-6
    if scale > 0 and abs(known) < 1e-3:
        return abs(abs(claimed) - abs(known)) / scale <= 1e-2
    return False


def verify(
    narrative: str,
    citable: dict[str, float],
    *,
    tolerance: float = 0.01,
) -> VerificationResult:
    """Check that every numeral in `narrative` resolves to a value in `citable`.

    Tolerance is absolute for small magnitudes and relative for large ones, so a
    figure rendered as "$1,550,000.00" still matches 1550000.0 and a margin
    rendered as "26.9%" matches 26.900000000000002.
    """
    numerals = extract_numerals(narrative)
    unresolved: list[Numeral] = []
    matched: dict[str, str] = {}

    for numeral in numerals:
        allowed = display_tolerance(numeral.text, tolerance)
        hit = next(
            (name for name, value in citable.items() if _matches(numeral.value, value, allowed)),
            None,
        )
        if hit is None:
            unresolved.append(numeral)
        else:
            matched[numeral.text] = hit

    return VerificationResult(
        ok=not unresolved,
        numerals_found=len(numerals),
        numerals_resolved=len(numerals) - len(unresolved),
        unresolved=unresolved,
        matched=matched,
    )


class UnfaithfulNarrative(RuntimeError):
    """Raised when a rendered narrative cites a number nothing computed."""

    def __init__(self, result: VerificationResult) -> None:
        super().__init__(result.failure_message())
        self.result = result
