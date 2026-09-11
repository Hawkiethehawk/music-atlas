from __future__ import annotations

import unittest

import web_view_model


def _definitions() -> dict[str, dict[str, str]]:
    return {
        "style:metalcore.traditional": {"label": "传统金属核"},
        "style:metalcore.modern_alternative": {"label": "现代另类金属核"},
        "style:metalcore.progressive_djent": {"label": "渐进/ djent 金属核"},
        "style:post_rock": {"label": "后摇"},
        "style:shoegaze": {"label": "shoegaze"},
        "style:nu_metal": {"label": "nu metal"},
    }


def _profile(interest_id: str, style_mix: list[dict[str, object]]) -> dict[str, object]:
    return {
        "interest_id": interest_id,
        "track_count": 3,
        "style_mix": style_mix,
        "style_axes": {},
        "representative_tracks": [{"track_key": "k", "title": "曲目", "artist": "艺人"}],
        "share": 0.5,
    }


def _analysis(profiles: list[dict[str, object]], definitions: dict[str, dict[str, str]] | None = None) -> dict:
    source = _definitions() if definitions is None else definitions
    return {
        "style_analysis": {
            "interest_profiles": profiles,
            "style_definitions": [{"style_ref": ref, **body} for ref, body in source.items()],
        }
    }


class ShortStyleLabelTests(unittest.TestCase):
    def test_strips_english_and_separators(self) -> None:
        self.assertEqual(web_view_model._short_style_label("渐进/ djent 金属核"), "渐进金属核")

    def test_keeps_tail_core_word_when_too_long(self) -> None:
        self.assertEqual(web_view_model._short_style_label("现代另类金属核"), "另类金属核")

    def test_returns_empty_for_english_only_labels(self) -> None:
        for label in ("nu metal", "screamo", "R&B", "lo-fi", "cloud rap"):
            with self.subTest(label=label):
                self.assertEqual(web_view_model._short_style_label(label), "")

    def test_keeps_short_label_unchanged(self) -> None:
        self.assertEqual(web_view_model._short_style_label("后摇"), "后摇")

    def test_handles_missing_label(self) -> None:
        for value in (None, "", 0, []):
            with self.subTest(value=value):
                self.assertEqual(web_view_model._short_style_label(value), "")


class DeriveInterestNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = {key: value for key, value in _definitions().items()}

    def test_prefers_highest_weight_eligible_style(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:post_rock", "weight": 0.9},          # 2 字，不合格
            {"style_ref": "style:metalcore.traditional", "weight": 0.3},
        ])

        self.assertEqual(web_view_model._derive_interest_name(profile, self.definitions, set()), "传统金属核")

    def test_truncates_overlong_style_label(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:metalcore.modern_alternative", "weight": 0.7},
        ])

        name = web_view_model._derive_interest_name(profile, self.definitions, set())

        self.assertEqual(name, "另类金属核")
        self.assertEqual(len(name), web_view_model.INTEREST_NAME_MAX_CHARS)

    def test_skips_names_already_used(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:metalcore.modern_alternative", "weight": 0.9},
            {"style_ref": "style:metalcore.traditional", "weight": 0.5},
        ])

        self.assertEqual(
            web_view_model._derive_interest_name(profile, self.definitions, {"另类金属核"}),
            "传统金属核",
        )

    def test_returns_empty_when_nothing_is_eligible(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:shoegaze", "weight": 1.0},
            {"style_ref": "style:nu_metal", "weight": 0.5},
            {"style_ref": "style:post_rock", "weight": 0.4},
        ])

        self.assertEqual(web_view_model._derive_interest_name(profile, self.definitions, set()), "")

    def test_ignores_unknown_style_refs(self) -> None:
        profile = _profile("interest-a", [{"style_ref": "style:does_not_exist", "weight": 1.0}])

        self.assertEqual(web_view_model._derive_interest_name(profile, self.definitions, set()), "")


class InterestEntryNamingTests(unittest.TestCase):
    def test_derives_distinct_names_from_style_mix(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.6}]),
            _profile("interest-b", [{"style_ref": "style:metalcore.modern_alternative", "weight": 0.6}]),
        ])

        entries = web_view_model._interest_entries(analysis, {})

        self.assertEqual([entry["name"] for entry in entries], ["传统金属核", "另类金属核"])

    def test_same_dominant_style_still_yields_distinct_names(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}]),
            _profile("interest-b", [
                {"style_ref": "style:metalcore.traditional", "weight": 0.9},
                {"style_ref": "style:metalcore.modern_alternative", "weight": 0.4},
            ]),
        ])

        names = [entry["name"] for entry in web_view_model._interest_entries(analysis, {})]

        self.assertEqual(names, ["传统金属核", "另类金属核"])

    def test_editorial_name_wins_and_keeps_code(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}]),
        ])
        editorial = {"interests": {"interest-a": {"name": "手写名字", "code": "07"}}}

        entries = web_view_model._interest_entries(analysis, editorial)

        self.assertEqual(entries[0]["name"], "手写名字")
        self.assertEqual(entries[0]["code"], "07")

    def test_auto_names_step_aside_for_editorial_names(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [
                {"style_ref": "style:metalcore.traditional", "weight": 0.9},
                {"style_ref": "style:metalcore.modern_alternative", "weight": 0.4},
            ]),
            _profile("interest-b", [{"style_ref": "style:metalcore.modern_alternative", "weight": 0.9}]),
        ])
        editorial = {"interests": {"interest-b": {"name": "传统金属核"}}}

        names = [entry["name"] for entry in web_view_model._interest_entries(analysis, editorial)]

        self.assertEqual(names, ["另类金属核", "传统金属核"])

    def test_falls_back_when_no_style_definitions(self) -> None:
        analysis = _analysis(
            [_profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}])],
            definitions={},
        )

        entries = web_view_model._interest_entries(analysis, {})

        self.assertEqual(entries[0]["name"], "兴趣组 01")

    def test_every_derived_name_is_three_to_five_characters(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}]),
            _profile("interest-b", [{"style_ref": "style:metalcore.progressive_djent", "weight": 0.9}]),
            _profile("interest-c", [{"style_ref": "style:metalcore.modern_alternative", "weight": 0.9}]),
        ])

        names = [entry["name"] for entry in web_view_model._interest_entries(analysis, {})]

        for name in names:
            with self.subTest(name=name):
                self.assertGreaterEqual(len(name), web_view_model.INTEREST_NAME_MIN_CHARS)
                self.assertLessEqual(len(name), web_view_model.INTEREST_NAME_MAX_CHARS)
        self.assertEqual(len(set(names)), len(names), "自动名字不应重复")


if __name__ == "__main__":
    unittest.main()
