from __future__ import annotations

import unittest

from contracts import ContractError
from musician_analyzer import _canonical_style_ref, _normalize_style_mix

KNOWN = {"style:pop_punk", "style:metalcore.traditional", "style:shoegaze"}


class CanonicalStyleRefTests(unittest.TestCase):
    def test_keeps_exact_reference(self) -> None:
        self.assertEqual(_canonical_style_ref("style:pop_punk", KNOWN), "style:pop_punk")

    def test_fixes_dot_separator(self) -> None:
        """关闭 reasoning 后模型偶发写成 style.pop_punk。"""

        self.assertEqual(_canonical_style_ref("style.pop_punk", KNOWN), "style:pop_punk")

    def test_fixes_space_and_slash_variants(self) -> None:
        for variant in ("style: pop_punk", "style :pop_punk", "style/pop_punk", "style . pop_punk"):
            with self.subTest(variant=variant):
                self.assertEqual(_canonical_style_ref(variant, KNOWN), "style:pop_punk")

    def test_fixes_case_and_whitespace(self) -> None:
        self.assertEqual(_canonical_style_ref("  STYLE:POP_PUNK  ", KNOWN), "style:pop_punk")

    def test_unknown_reference_is_left_for_the_contract(self) -> None:
        self.assertEqual(_canonical_style_ref("style:not_a_style", KNOWN), "style:not_a_style")

    def test_blank_value_stays_blank(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(_canonical_style_ref(value, KNOWN), "")


class NormalizeStyleMixTests(unittest.TestCase):
    def test_accepts_typo_and_keeps_contract(self) -> None:
        mix = _normalize_style_mix(
            [{"style_ref": "style.pop_punk", "role": "primary", "weight": 1.0}],
            "研究[0].style_mix",
            KNOWN,
        )

        self.assertEqual(mix[0]["style_ref"], "style:pop_punk")

    def test_unknown_reference_still_fails(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            _normalize_style_mix(
                [{"style_ref": "style.nope", "role": "primary", "weight": 0.5}],
                "研究[0].style_mix",
                KNOWN,
            )

        self.assertIn("未知风格引用", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
