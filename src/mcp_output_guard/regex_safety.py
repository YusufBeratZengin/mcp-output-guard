"""Static rejection of regular expressions that can backtrack catastrophically.

``re`` performs a match inside C code: it cannot be interrupted, cancelled or
given a timeout, so a pattern such as ``(a|aa)+$`` on a 4,000-character line of
"a"s wedges the calling thread for good. The only reliable defence is to refuse
the pattern *before* it runs, which is what :func:`pattern_risk` does by parsing
it with the interpreter's own regex parser and rejecting the two shapes that
make backtracking exponential:

* a quantifier nested inside another quantifier (``(a+)+``);
* an unbounded repetition of a group whose alternatives can match the same
  text (``(a|aa)+``, ``(foo|foobar)*``, a branch that can match nothing).

The analysis is deliberately conservative: when the first characters of a
repeated branch cannot be bounded (``\\d``, ``\\w``, ``.``, a negated class, a
backreference), the pattern is refused as well. A false "no" costs the caller a
rewrite; a false "yes" costs the caller the process.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["pattern_risk"]


def _get_sre_parse() -> Any:
    for mod_name in ("re._parser", "sre_parse"):
        try:
            return __import__(
                mod_name,
                fromlist=["parse", "MAX_REPEAT", "MIN_REPEAT", "SUBPATTERN", "BRANCH"],
            )
        except ImportError:
            continue
    return None


def _has_nested_repeat(node: Any, parser: Any, in_repeat: bool = False) -> bool:
    for item in node:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        op, av = item
        if op in (parser.MAX_REPEAT, parser.MIN_REPEAT):
            if in_repeat:
                return True
            if isinstance(av, tuple) and len(av) >= 3 and _has_nested_repeat(av[2], parser, in_repeat=True):
                return True
        elif op == parser.SUBPATTERN:
            sub = av[3] if len(av) == 4 else av[1]
            if _has_nested_repeat(sub, parser, in_repeat=in_repeat):
                return True
        elif op == parser.BRANCH:
            for branch in av[1]:
                if _has_nested_repeat(branch, parser, in_repeat=in_repeat):
                    return True
    return False


#: Reasons a pattern is refused before it is ever run. Both shapes make
#: ``re`` backtrack exponentially, and Python cannot interrupt one in-flight
#: C-level match, so the only reliable place to stop them is validation.
_NESTED_REPEAT_MESSAGE = (
    "regular expression contains nested repetitions that risk catastrophic backtracking; "
    "rewrite it without a quantifier inside another quantifier"
)
_AMBIGUOUS_ALTERNATION_MESSAGE = (
    "regular expression repeats a group whose alternatives can match the same text "
    "(overlapping alternatives, or a branch that can match nothing), which risks "
    "catastrophic backtracking; replace the group with a character class or make the "
    "alternatives disjoint"
)

#: Fallbacks used only when this interpreter exposes no regex parse tree. They
#: are deliberately blunt: without a parse tree the honest answer is "refuse".
_FALLBACK_NESTED = re.compile(r"\((?:[^\(\)]*[\+\*\{]|\S+?[\+\*\{]).*?\)\s*(?:[\+\*]|\{\d+,\d*\})")
_FALLBACK_ALTERNATION = re.compile(r"\([^()]*\|[^()]*\)\s*(?:\+|\*|\{\d+,\d*\})")

#: Below this many iterations a repeated branch cannot blow up: even the worst
#: case stays at 2**8 paths over a 4,000-character line.
_SAFE_REPEAT_CEILING = 8


def _subpattern_body(value: Any) -> Any:
    """Return the parsed body of a ``SUBPATTERN`` item across Python versions."""
    return value[3] if len(value) == 4 else value[1]


def _fold_chars(char: int, fold: bool) -> frozenset[int]:
    """One literal character, plus its case variants when ``ignore_case`` is on."""
    found = {char}
    if fold:
        text = chr(char)
        for variant in (text.lower(), text.upper()):
            if len(variant) == 1:
                found.add(ord(variant))
    return frozenset(found)


def _class_first(items: Any, parser: Any, fold: bool) -> frozenset[int] | None:
    """First characters of a character class, or ``None`` when unbounded."""
    chars: set[int] = set()
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            return None
        op, value = item
        if op == getattr(parser, "LITERAL", None):
            chars |= _fold_chars(value, fold)
        elif op == getattr(parser, "RANGE", None):
            low, high = value
            if high - low > 512:
                return None
            for char in range(low, high + 1):
                chars |= _fold_chars(char, fold)
        else:
            # NEGATE, CATEGORY (\d, \w, ...) and BIGCHARSET describe an
            # open-ended set; treating it as unknown is the safe answer.
            return None
    return frozenset(chars)


def _branch_first(branches: Any, parser: Any, fold: bool) -> tuple[frozenset[int] | None, bool]:
    """First characters shared by a whole ``BRANCH``, plus its nullability."""
    chars: set[int] = set()
    nullable = False
    for branch in branches:
        first, branch_nullable = _first_chars(branch, parser, fold)
        if first is None:
            return None, False
        chars |= first
        nullable = nullable or branch_nullable
    return frozenset(chars), nullable


def _item_first(op: Any, value: Any, parser: Any, fold: bool) -> tuple[frozenset[int] | None, bool]:
    """First characters of one parsed item, and whether it can match nothing."""
    if op == getattr(parser, "LITERAL", None):
        return _fold_chars(value, fold), False
    if op in (getattr(parser, "NOT_LITERAL", None), getattr(parser, "ANY", None), getattr(parser, "ANY_ALL", None)):
        return None, False
    if op == getattr(parser, "IN", None):
        return _class_first(value, parser, fold), False
    if op == getattr(parser, "BRANCH", None):
        return _branch_first(value[1], parser, fold)
    if op == getattr(parser, "SUBPATTERN", None):
        return _first_chars(_subpattern_body(value), parser, fold)
    if op in (getattr(parser, "MAX_REPEAT", None), getattr(parser, "MIN_REPEAT", None)):
        low, _high, body = value
        first, body_nullable = _first_chars(body, parser, fold)
        return first, (low == 0 or body_nullable)
    # GROUPREF, GROUPREF_EXISTS, atomic groups, conditionals: unknown.
    return None, False


def _first_chars(node: Any, parser: Any, fold: bool) -> tuple[frozenset[int] | None, bool]:
    """Characters a sequence can start with, or ``None`` if it is open-ended.

    Returned alongside is nullability: whether the sequence can match the empty
    string. Both answers over-approximate, which is the right direction for a
    rejection check.
    """
    chars: set[int] = set()
    nullable = True
    for item in node:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        op, value = item
        if op in (
            getattr(parser, "AT", None),
            getattr(parser, "ASSERT", None),
            getattr(parser, "ASSERT_NOT", None),
        ):
            continue
        first, item_nullable = _item_first(op, value, parser, fold)
        if first is None:
            return None, False
        chars |= first
        if not item_nullable:
            nullable = False
            break
    return frozenset(chars), nullable


def _branches_overlap(branches: Any, parser: Any, fold: bool) -> bool:
    """True if two alternatives of one branch can consume the same text."""
    if len(branches) < 2:
        return False
    heads: list[frozenset[int]] = []
    for branch in branches:
        first, nullable = _first_chars(branch, parser, fold)
        # A branch that can match nothing gives the surrounding repetition two
        # ways to consume the same input, which is just as explosive.
        if nullable or first is None:
            return True
        heads.append(first)
    for index, left in enumerate(heads):
        for right in heads[index + 1 :]:
            if left & right:
                return True
    return False


def _has_branch_conflict(node: Any, parser: Any, fold: bool) -> bool:
    """True if any ``BRANCH`` under *node* has overlapping alternatives."""
    for item in node:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        op, value = item
        if op == getattr(parser, "BRANCH", None):
            if _branches_overlap(value[1], parser, fold):
                return True
            if any(_has_branch_conflict(branch, parser, fold) for branch in value[1]):
                return True
        elif op == getattr(parser, "SUBPATTERN", None):
            if _has_branch_conflict(_subpattern_body(value), parser, fold):
                return True
        elif op in (getattr(parser, "MAX_REPEAT", None), getattr(parser, "MIN_REPEAT", None)) and (
            _has_branch_conflict(value[2], parser, fold)
        ):
            return True
    return False


def _has_ambiguous_repeat(node: Any, parser: Any, fold: bool) -> bool:
    """True if an unbounded repetition repeats a group with overlapping branches."""
    for item in node:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        op, value = item
        if op in (getattr(parser, "MAX_REPEAT", None), getattr(parser, "MIN_REPEAT", None)):
            _low, high, body = value
            if high >= _SAFE_REPEAT_CEILING and _has_branch_conflict(body, parser, fold):
                return True
            if _has_ambiguous_repeat(body, parser, fold):
                return True
        elif op == getattr(parser, "SUBPATTERN", None):
            if _has_ambiguous_repeat(_subpattern_body(value), parser, fold):
                return True
        elif op == getattr(parser, "BRANCH", None) and any(
            _has_ambiguous_repeat(branch, parser, fold) for branch in value[1]
        ):
            return True
    return False


def pattern_risk(pattern: str, flags: int = 0) -> str | None:
    """Return a rejection message if *pattern* may backtrack catastrophically.

    Two shapes are refused before any input is touched: a quantifier nested in
    another quantifier, and an unbounded repetition of a group whose branches
    can match the same text. Patterns the parse tree cannot describe (an
    unknown character set inside a repeated branch) are refused too, because a
    conservative "no" is the only safe answer when the runtime cannot be
    interrupted mid-match.
    """
    parser = _get_sre_parse()
    if parser is not None:
        try:
            parsed = parser.parse(pattern, flags)
        except Exception:
            # Not describable here; ``re.compile`` reports the syntax error.
            return None
        fold = bool(flags & re.IGNORECASE)
        if _has_nested_repeat(parsed, parser):
            return _NESTED_REPEAT_MESSAGE
        if _has_ambiguous_repeat(parsed, parser, fold):
            return _AMBIGUOUS_ALTERNATION_MESSAGE
        return None
    if _FALLBACK_NESTED.search(pattern) or _FALLBACK_ALTERNATION.search(pattern):
        return _NESTED_REPEAT_MESSAGE
    return None
