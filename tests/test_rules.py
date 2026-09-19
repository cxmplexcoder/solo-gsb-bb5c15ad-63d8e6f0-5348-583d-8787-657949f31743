"""Unit tests for rule patterns: compilation, matching and rule selection."""

import unittest

from app.merge import parse_pointer
from app.rules import (
    InvalidRulePattern,
    compile_pattern,
    pattern_matches,
    select_rules,
)


def match(pattern: str, pointer: str) -> bool:
    cp = compile_pattern(pattern)
    return pattern_matches(cp, tuple(parse_pointer(pointer)))


def rules(*specs):
    """specs: (pattern, choice) -> compiled list understood by select_rules."""
    return [(i, compile_pattern(p), choice) for i, (p, choice) in enumerate(specs)]


def conflicts(*pointers, missing=()):
    return [
        {"pointer": p, "missing": list(missing) if p in missing else []}
        for p in pointers
    ]


class CompilePatternTest(unittest.TestCase):
    def test_root_pattern(self):
        cp = compile_pattern("")
        self.assertEqual(cp.tokens, ())
        self.assertFalse(cp.recursive)

    def test_literal_and_wildcard_tokens(self):
        cp = compile_pattern("/db/*/host")
        self.assertEqual(cp.tokens, ("db", "*", "host"))
        self.assertFalse(cp.recursive)

    def test_trailing_recursive(self):
        cp = compile_pattern("/db/**")
        self.assertEqual(cp.tokens, ("db",))
        self.assertTrue(cp.recursive)

    def test_rfc6901_escapes_are_unescaped(self):
        cp = compile_pattern("/a~1b/c~0d")
        self.assertEqual(cp.tokens, ("a/b", "c~d"))

    def test_non_string_pattern(self):
        with self.assertRaises(InvalidRulePattern):
            compile_pattern(123)

    def test_missing_leading_slash(self):
        with self.assertRaises(InvalidRulePattern):
            compile_pattern("db/host")

    def test_star_must_fill_segment(self):
        with self.assertRaises(InvalidRulePattern):
            compile_pattern("/db/host-*")

    def test_double_star_only_as_final_segment(self):
        with self.assertRaises(InvalidRulePattern):
            compile_pattern("/db/**/host")

    def test_bad_tilde_escape(self):
        with self.assertRaises(InvalidRulePattern):
            compile_pattern("/db/ho~st")
        with self.assertRaises(InvalidRulePattern):
            compile_pattern("/db/~2host")

    def test_empty_segment_is_a_literal_empty_key(self):
        cp = compile_pattern("/a//b")
        self.assertEqual(cp.tokens, ("a", "", "b"))


class PatternMatchTest(unittest.TestCase):
    def test_literal_match(self):
        self.assertTrue(match("/db/host", "/db/host"))
        self.assertFalse(match("/db/host", "/db/port"))
        self.assertFalse(match("/db/host", "/db/host/extra"))

    def test_root_match(self):
        self.assertTrue(match("", ""))
        self.assertFalse(match("", "/db"))

    def test_single_star_matches_exactly_one_segment(self):
        self.assertTrue(match("/db/*", "/db/host"))
        self.assertFalse(match("/db/*", "/db"))
        self.assertFalse(match("/db/*", "/db/host/tls"))

    def test_double_star_matches_zero_or_more_segments(self):
        self.assertTrue(match("/db/**", "/db"))
        self.assertTrue(match("/db/**", "/db/host"))
        self.assertTrue(match("/db/**", "/db/x/y/z"))
        self.assertFalse(match("/db/**", "/other/host"))
        self.assertTrue(match("/**", "/anything/at/all"))
        self.assertTrue(match("/**", ""))

    def test_escaped_literal_matching(self):
        self.assertTrue(match("/a~1b", "/a~1b"))
        self.assertFalse(match("/a~1b", "/axb"))


class SelectRulesTest(unittest.TestCase):
    def test_each_conflict_gets_unique_rule(self):
        cs = conflicts("/db/host", "/db/port", "/image")
        assignments, hits, unmatched = select_rules(
            cs, rules(("/db/*", "left"), ("/image", "right"))
        )
        self.assertEqual(assignments["/db/host"]["rule_index"], 0)
        self.assertEqual(assignments["/db/host"]["choice"], "left")
        self.assertEqual(assignments["/db/port"]["rule_index"], 0)
        self.assertEqual(assignments["/image"]["rule_index"], 1)
        self.assertEqual(hits[0], ["/db/host", "/db/port"])
        self.assertEqual(hits[1], ["/image"])
        self.assertEqual(unmatched, [])

    def test_more_literal_segments_beat_wildcards(self):
        cs = conflicts("/db/host")
        assignments, _, _ = select_rules(
            cs,
            rules(("/db/*", "right"), ("/db/host", "left"), ("/**", "right")),
        )
        self.assertEqual(assignments["/db/host"]["rule_index"], 1)

    def test_star_outranks_double_star(self):
        # /db/* -> 1 literal, 1 star; /db/** -> 1 literal, 2 stars.
        cs = conflicts("/db/host")
        assignments, _, _ = select_rules(
            cs, rules(("/db/**", "right"), ("/db/*", "left"))
        )
        self.assertEqual(assignments["/db/host"]["rule_index"], 1)
        self.assertEqual(assignments["/db/host"]["choice"], "left")

        # Declaration order cannot save a less specific pattern.
        assignments, _, _ = select_rules(
            cs, rules(("/db/*", "left"), ("/db/**", "right"))
        )
        self.assertEqual(assignments["/db/host"]["rule_index"], 0)
        self.assertEqual(assignments["/db/host"]["choice"], "left")

    def test_star_beats_shorter_recursive_pattern(self):
        # /** has 0 literals + 2 stars; /db/* has 1 literal + 1 star.
        cs = conflicts("/db/host")
        assignments, _, _ = select_rules(
            cs, rules(("/**", "right"), ("/db/*", "left"))
        )
        self.assertEqual(assignments["/db/host"]["rule_index"], 1)

    def test_tie_resolved_by_declaration_order(self):
        cs = conflicts("/db/host")
        assignments, hits, _ = select_rules(
            cs, rules(("/db/*", "left"), ("/db/*", "right"))
        )
        self.assertEqual(assignments["/db/host"]["rule_index"], 0)
        self.assertEqual(assignments["/db/host"]["choice"], "left")
        self.assertEqual(hits[0], ["/db/host"])
        self.assertEqual(hits[1], [])

    def test_unmatched_conflicts_are_returned(self):
        cs = conflicts("/db/host", "/other")
        _, hits, unmatched = select_rules(cs, rules(("/db/host", "left")))
        self.assertEqual([c["pointer"] for c in unmatched], ["/other"])
        self.assertEqual(hits[0], ["/db/host"])

    def test_recursive_pattern_can_match_zero_segments(self):
        cs = conflicts("/db", "/db/host", "/db/x/tls")
        assignments, hits, unmatched = select_rules(cs, rules(("/db/**", "right")))
        self.assertEqual(sorted(assignments), ["/db", "/db/host", "/db/x/tls"])
        self.assertEqual(hits[0], ["/db", "/db/host", "/db/x/tls"])
        self.assertEqual(unmatched, [])

    def test_non_matching_rule_keeps_empty_hits_entry(self):
        cs = conflicts("/db/host")
        _, hits, unmatched = select_rules(
            cs, rules(("/image", "left"), ("/db/*", "right"))
        )
        self.assertEqual(hits[0], [])
        self.assertEqual(hits[1], ["/db/host"])
        self.assertEqual(unmatched, [])


if __name__ == "__main__":
    unittest.main()
