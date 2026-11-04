"""The regex guard must refuse exponential patterns before touching any input.

A single in-flight C-level ``re`` match cannot be interrupted, so the per-line
wall-clock budget is useless against a pattern that backtracks exponentially.
The pattern checker therefore has to be right *before* the search starts; these
tests pin down both what it refuses and what it must keep accepting.
"""

from __future__ import annotations

import pytest

from mcp_output_guard import regex_safety
from mcp_output_guard.spill import SpillError, SpillStore


def test_ordinary_patterns_are_accepted() -> None:
    for pattern in (
        "error",
        r"^\w+=\d+$",
        "(foo|bar)",
        "a+b*c?",
        r"\[[0-9]{4}\]",
        "(?:ab)+",
        r"\bGET\s+/api/\w+",
    ):
        assert regex_safety.pattern_risk(pattern) is None, pattern


def test_lazy_and_branch_nested_repeats_are_still_checked() -> None:
    # A lazy quantifier is a MIN_REPEAT node; the repeat hidden in a top-level
    # branch must not escape the walk either.
    assert regex_safety.pattern_risk("(a|aa)+?b") is not None
    assert regex_safety.pattern_risk("(?:(a|aa)+|zzz)") is not None


def test_grep_refuses_before_reading_the_file(store: SpillStore) -> None:
    # The payload is long enough that an exponential match would never return;
    # raising immediately is the observable proof that no match was attempted.
    record = store.put_text("a" * 4_000 + "!")
    with pytest.raises(SpillError, match="alternatives"):
        store.grep(record.spill_id, "(a|aa)+$", budget_seconds=0.001)


def test_character_class_analysis() -> None:
    parser = regex_safety._get_sre_parse()
    assert parser is not None
    items = [(parser.LITERAL, ord("a")), (parser.RANGE, (ord("b"), ord("d")))]
    assert regex_safety._class_first(items, parser, False) == frozenset(b"abcd")
    # Case folding widens a literal, which is what makes "(A|ab)+" ambiguous
    # under ignore_case.
    assert regex_safety._class_first([(parser.LITERAL, ord("a"))], parser, True) == frozenset(b"aA")
    # Open-ended classes and anything unexpected are reported as unknown, and
    # "unknown" means "refuse".
    assert regex_safety._class_first([(parser.RANGE, (0, 0x2FFFF))], parser, False) is None
    assert regex_safety._class_first([(parser.CATEGORY, None)], parser, False) is None
    assert regex_safety._class_first([(parser.NEGATE, None)], parser, False) is None
    assert regex_safety._class_first([("surprise",)], parser, False) is None


def test_folding_a_character_without_a_single_codepoint_uppercase() -> None:
    # "ß".upper() is "SS": no single-character variant exists, so folding must
    # not invent one.
    assert regex_safety._fold_chars(ord("\u00df"), True) == frozenset({ord("\u00df")})


def test_subpattern_body_supports_both_parse_shapes() -> None:
    assert regex_safety._subpattern_body((1, 0, 0, ["body"])) == ["body"]
    assert regex_safety._subpattern_body((1, ["body"])) == ["body"]


def test_unknown_opcodes_are_treated_as_open_ended() -> None:
    parser = regex_safety._get_sre_parse()
    assert parser is not None
    assert regex_safety._item_first(parser.GROUPREF, 1, parser, False) == (None, False)
    assert regex_safety._item_first(parser.NOT_LITERAL, ord("a"), parser, False) == (None, False)


def test_first_char_walk_follows_nullable_prefixes() -> None:
    parser = regex_safety._get_sre_parse()
    assert parser is not None
    # "x?" can match nothing, so the branch behind it really is reachable and
    # has to be analysed instead of being hidden by the first item.
    first, nullable = regex_safety._first_chars(parser.parse("(?:x?)(a|b)"), parser, False)
    assert first == frozenset(b"xab")
    assert nullable is False
    # Same for a nested group and for a zero-minimum repeat.
    first = regex_safety._first_chars(parser.parse("(?:x?)((a))"), parser, False)[0]
    assert first == frozenset(b"xa")
    first = regex_safety._first_chars(parser.parse("(?:y*)(z)"), parser, False)[0]
    assert first == frozenset(b"yz")
    # A branch can also be open-ended; that answers "unknown".
    branches = [[(parser.CATEGORY, None)], [(parser.LITERAL, ord("a"))]]
    assert regex_safety._branch_first(branches, parser, False) == (None, False)


def test_parser_free_fallback_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regex_safety, "_get_sre_parse", lambda: None)
    assert regex_safety.pattern_risk("(a|aa)+") is not None
    assert regex_safety.pattern_risk("(a+)+$") is not None
    assert regex_safety.pattern_risk("plain text") is None


def test_unparseable_pattern_is_left_to_re_compile(store: SpillStore) -> None:
    assert regex_safety.pattern_risk("([") is None
    record = store.put_text("abc")
    with pytest.raises(SpillError, match="invalid regular expression"):
        store.grep(record.spill_id, "([")
