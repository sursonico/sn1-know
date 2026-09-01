import unittest

from kb.db import split_multi_value


class SplitMultiValueTests(unittest.TestCase):
    def test_plain_compound(self):
        self.assertEqual(split_multi_value("Turkey, Ukraine"), ["Turkey", "Ukraine"])

    def test_slash_compound(self):
        self.assertEqual(split_multi_value("Nine/Stan Sport"), ["Nine", "Stan Sport"])

    def test_semicolon_compound(self):
        self.assertEqual(split_multi_value("Europe; Asia; Oceania"), ["Europe", "Asia", "Oceania"])

    def test_parenthetical_stays_whole(self):
        self.assertEqual(split_multi_value("Disney (ABC/ESPN)"), ["Disney (ABC/ESPN)"])

    def test_parenthetical_with_comma_stays_whole(self):
        self.assertEqual(
            split_multi_value("Latin America (ex Brazil, Mexico)"),
            ["Latin America (ex Brazil, Mexico)"],
        )

    def test_bracketed_stays_whole(self):
        self.assertEqual(split_multi_value("AMC Networks (Sport1/Sport2)"), ["AMC Networks (Sport1/Sport2)"])

    def test_nested_brackets_stay_whole(self):
        self.assertEqual(
            split_multi_value("Foo [Bar (X, Y), Baz]"),
            ["Foo [Bar (X, Y), Baz]"],
        )

    def test_delimiter_outside_and_inside_brackets(self):
        # Splits at depth zero, keeps the bracketed part intact.
        self.assertEqual(
            split_multi_value("UK, Disney (ABC/ESPN), France"),
            ["UK", "Disney (ABC/ESPN)", "France"],
        )

    def test_square_brackets_alone(self):
        self.assertEqual(split_multi_value("Territory [A, B]"), ["Territory [A, B]"])

    def test_empty_and_blank(self):
        self.assertEqual(split_multi_value(""), [])
        self.assertEqual(split_multi_value(None), [])
        self.assertEqual(split_multi_value("   "), [])

    def test_single_value_no_delimiters(self):
        self.assertEqual(split_multi_value("Spain"), ["Spain"])

    def test_mismatched_closing_bracket_does_not_crash(self):
        # Depth clamps at zero rather than going negative; degrades to
        # "don't split here" instead of raising.
        result = split_multi_value("Foo) Bar, Baz")
        self.assertTrue(all(isinstance(p, str) for p in result))


if __name__ == "__main__":
    unittest.main()
