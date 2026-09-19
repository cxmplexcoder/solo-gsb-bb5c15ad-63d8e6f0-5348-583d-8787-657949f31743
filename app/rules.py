"""RFC 6901-style path patterns for rule-based conflict resolution.

A rule points at conflict locations (given as RFC 6901 JSON Pointers, see
RFC 6901) using a pointer-shaped *pattern*:

  * literal segments follow RFC 6901 escaping (``~0`` -> ``~``, ``~1`` -> ``/``)
  * a segment that is a single ``*`` matches exactly one pointer segment
  * a trailing ``**`` segment matches zero or more remaining segments; it
    is only legal as the final segment
  * the empty string ``""`` is the root pattern and matches a conflict at
    the document root

A bare ``*`` may not be combined with other characters inside a segment
(``/a-*`` is rejected), and ``~`` must still be followed by ``0``/``1``.

Rule selection for every open conflict is deterministic and unique:
more literal segments win, then fewer ``*`` characters (a single ``*``
segment counts one, the trailing ``**`` counts two), then the earlier
declaration order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .merge import parse_pointer

WILD = "*"
RECURSIVE = "**"


class InvalidRulePattern(ValueError):
    """A rule uses a malformed pointer pattern."""


@dataclass(frozen=True)
class CompiledPattern:
    pattern: str
    tokens: tuple[str, ...]
    recursive: bool  # pattern ends with **


def _unescape_segment(raw: str, pattern: str) -> str:
    """Unescape one RFC 6901 reference token, rejecting bad ``~`` escapes."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "~":
            if i + 1 >= len(raw) or raw[i + 1] not in "01":
                raise InvalidRulePattern(
                    f"invalid escape in pattern {pattern!r}: '~' must be followed by 0 or 1"
                )
            out.append("~" if raw[i + 1] == "0" else "/")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def compile_pattern(pattern: Any) -> CompiledPattern:
    """Validate and compile one rule pattern into matchable tokens."""
    if not isinstance(pattern, str):
        raise InvalidRulePattern("rule pattern must be a string")
    if pattern == "":
        return CompiledPattern("", (), False)
    if not pattern.startswith("/"):
        raise InvalidRulePattern(
            f"pattern {pattern!r} must start with '/' (or be '' for the document root)"
        )
    raw_tokens = pattern.split("/")[1:]
    tokens: list[str] = []
    recursive = False
    for i, raw in enumerate(raw_tokens):
        if raw == WILD:
            tokens.append(WILD)
            continue
        if raw == RECURSIVE:
            if i != len(raw_tokens) - 1:
                raise InvalidRulePattern(
                    f"'**' is only allowed as the final segment of pattern {pattern!r}"
                )
            recursive = True
            continue
        if WILD in raw:
            raise InvalidRulePattern(
                f"'*' must occupy a whole segment in pattern {pattern!r}"
            )
        tokens.append(_unescape_segment(raw, pattern))
    return CompiledPattern(pattern, tuple(tokens), recursive)


def pattern_matches(cp: CompiledPattern, pointer_tokens: tuple[str, ...]) -> bool:
    """Whether a compiled pattern matches an unescaped conflict pointer."""
    fixed = len(cp.tokens)
    if cp.recursive:
        # The trailing ** absorbs zero or more remaining segments.
        if len(pointer_tokens) < fixed:
            return False
    elif len(pointer_tokens) != fixed:
        return False
    return all(
        pat == WILD or pat == tok
        for pat, tok in zip(cp.tokens, pointer_tokens)
    )


def _specificity(cp: CompiledPattern) -> tuple[int, int]:
    """Higher specificity first: literals (more), ``*`` (fewer).

    A single ``*`` segment counts one ``*``; the trailing ``**`` counts
    two, so a fixed-length pattern always outranks a recursive one even
    though the latter can match zero tail segments. Fully tied rules
    resolve by declaration order.
    """
    single_stars = sum(1 for tok in cp.tokens if tok == WILD)
    literals = len(cp.tokens) - single_stars
    stars = single_stars + (2 if cp.recursive else 0)
    return literals, stars


def select_rules(
    conflicts: list[dict], compiled_rules: list[tuple[int, CompiledPattern, str]]
) -> tuple[dict[str, dict], dict[int, list[str]], list[dict]]:
    """Pick the unique winning rule for every open conflict.

    ``compiled_rules`` is a list of ``(declaration_index, pattern, choice)``
    in declaration order. Returns:

      * ``assignments``: ``pointer -> {"pointer", "choice", "rule_index"}``
        for every conflict matched by at least one rule;
      * ``hits``: ``rule_index -> pointers`` hit by that rule (pointers in
        conflict-list order; every declared rule has an entry);
      * ``unmatched``: conflict objects no rule matched.
    """
    assignments: dict[str, dict] = {}
    hits: dict[int, list[str]] = {idx: [] for idx, _, _ in compiled_rules}
    unmatched: list[dict] = []

    for conflict in conflicts:
        pointer = conflict["pointer"]
        tokens = tuple(parse_pointer(pointer))
        best_key: tuple[int, int, int] | None = None
        best_idx = -1
        best_choice = ""
        for idx, cp, choice in compiled_rules:
            if not pattern_matches(cp, tokens):
                continue
            literals, wildcards = _specificity(cp)
            # Negate so that smaller is better: more literals, fewer
            # wildcards, earlier declaration.
            key = (-literals, wildcards, idx)
            if best_key is None or key < best_key:
                best_key, best_idx, best_choice = key, idx, choice
        if best_key is None:
            unmatched.append(conflict)
            continue
        assignments[pointer] = {
            "pointer": pointer,
            "choice": best_choice,
            "rule_index": best_idx,
        }
        hits[best_idx].append(pointer)

    return assignments, hits, unmatched
