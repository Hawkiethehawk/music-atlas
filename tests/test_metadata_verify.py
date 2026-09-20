from __future__ import annotations

import os
import unittest
from unittest import mock

import metadata_verify as mv


def _hit(title: str, artist: str, **overrides) -> dict:
    value = {
        "title": title,
        "artist": artist,
        "artists": [artist],
        "album": "",
        "cover": None,
        "platform_id": "",
        "url": None,
    }
    value.update(overrides)
    return value


class NormalizeTests(unittest.TestCase):
    def test_case_width_and_spacing(self) -> None:
        self.assertEqual(mv.normalize_name("Ｈｅｌｌｏ　ＷＯＲＬＤ"), "hello world")

    def test_drops_brackets_versions_and_feat(self) -> None:
        for value in ("Song (Live)", "Song (feat. Someone)", "Song - Remastered 2011", "Song [Deluxe]"):
            with self.subTest(value=value):
                self.assertEqual(mv.normalize_name(value), "song")

    def test_keeps_cjk(self) -> None:
        self.assertEqual(mv.normalize_name("夜曲（Live）"), "夜曲")
        self.assertEqual(mv.normalize_name("七里香 - 现场版"), "七里香 现场版")

    def test_empty_value(self) -> None:
        self.assertEqual(mv.normalize_name(None), "")


class SimilarityTests(unittest.TestCase):
    def test_identical_and_contained_names(self) -> None:
        self.assertEqual(mv.name_similarity("Song", "song"), 1.0)
        self.assertGreaterEqual(mv.name_similarity("Song", "Song (Live)"), 0.9)

    def test_unrelated_names_score_low(self) -> None:
        self.assertLess(mv.name_similarity("Yesterday", "Bohemian Rhapsody"), 0.5)
        self.assertLess(mv.name_similarity("夜曲", "晴天"), 0.5)

    def test_missing_side_scores_zero(self) -> None:
        self.assertEqual(mv.name_similarity("", "x"), 0.0)
        self.assertEqual(mv.name_similarity("x", None), 0.0)


class SwitchTests(unittest.TestCase):
    def test_disabled_keeps_original_values(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_METADATA_VERIFY": "off"}), \
             mock.patch.object(mv, "SOURCES", {}):
            result = mv.verify_candidate("曲目", "艺人")

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "skipped")
        self.assertEqual(result["title"], "曲目")
        self.assertEqual(result["artist"], "艺人")

    def test_enabled_by_default(self) -> None:
        clean = {key: value for key, value in os.environ.items() if key != "ATLAS_METADATA_VERIFY"}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertTrue(mv.verification_enabled())


class MatchTests(unittest.TestCase):
    def _verify(self, title: str, artist: str, sources: dict) -> dict | None:
        with mock.patch.object(mv, "verification_enabled", return_value=True), \
             mock.patch.object(mv, "SOURCES", sources), \
             mock.patch.object(mv, "SOURCE_PRIORITY", tuple(sources)), \
             mock.patch.object(mv, "cover_url_status", return_value="verified"):
            return mv.verify_candidate(title, artist)

    def test_picks_best_hit_and_returns_platform_fields(self) -> None:
        hits = [
            _hit("别的歌", "别的艺人"),
            _hit("夜曲", "周杰伦", album="十一月的萧邦", cover="https://cover.example/night.jpg",
                 platform_id="9", url="https://music.163.com/song?id=9"),
        ]
        result = self._verify("夜曲", "周杰伦", {"netease": lambda title, artist: hits})

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "netease")
        self.assertEqual(result["album"], "十一月的萧邦")
        self.assertEqual(result["cover"], "https://cover.example/night.jpg")
        self.assertEqual(result["platform_track_id"], "9")
        self.assertGreaterEqual(result["score"], mv.MIN_MATCH_SCORE)


    def test_unreachable_primary_cover_uses_verified_fallback(self) -> None:
        calls: list[str] = []
        def netease(title: str, artist: str) -> list[dict]:
            calls.append("netease")
            return [_hit("Sympathy", "Too Close To Touch", album="Haven't Been Myself",
                          cover="https://invalid.example/sympathy.jpg", platform_id="431855775",
                          url="https://music.163.com/song?id=431855775")]
        def itunes(title: str, artist: str) -> list[dict]:
            calls.append("itunes")
            return [_hit("Sympathy", "Too Close To Touch", album="Haven't Been Myself",
                          cover="https://itunes.example/sympathy.jpg", platform_id="123",
                          url="https://music.apple.com/us/song/123")]
        def status(url: str) -> str:
            return "unreachable" if "invalid" in url else "verified"
        with mock.patch.object(mv, "verification_enabled", return_value=True), \
             mock.patch.object(mv, "SOURCES", {"netease": netease, "itunes": itunes}), \
             mock.patch.object(mv, "SOURCE_PRIORITY", ("netease", "itunes")), \
             mock.patch.object(mv, "cover_url_status", side_effect=status):
            result = mv.verify_candidate("Sympathy", "Too Close To Touch")
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "netease")
        self.assertEqual(result["platform_track_id"], "431855775")
        self.assertEqual(result["cover"], "https://itunes.example/sympathy.jpg")
        self.assertEqual(result["cover_source"], "itunes")
        self.assertEqual(calls, ["netease", "itunes"])

    def test_unreachable_all_covers_keeps_identity(self) -> None:
        sources = {
            "netease": lambda title, artist: [_hit("Sympathy", "Too Close To Touch", cover="https://bad/1", platform_id="1", url="https://n/1")],
            "itunes": lambda title, artist: [_hit("Sympathy", "Too Close To Touch", cover="https://bad/2", platform_id="2", url="https://i/2")],
        }
        with mock.patch.object(mv, "verification_enabled", return_value=True), \
             mock.patch.object(mv, "SOURCES", sources), \
             mock.patch.object(mv, "SOURCE_PRIORITY", ("netease", "itunes")), \
             mock.patch.object(mv, "cover_url_status", return_value="unreachable"):
            result = mv.verify_candidate("Sympathy", "Too Close To Touch")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Sympathy")
        self.assertEqual(result["artist"], "Too Close To Touch")
        self.assertIsNone(result["cover"])

    def test_cover_url_status_is_cached(self) -> None:
        calls = []
        class Response:
            status = 200
            def getcode(self): return 200
            class Headers:
                def get(self, key, default=""): return "image/jpeg"
            headers = Headers()
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def fake_open(*args, **kwargs):
            calls.append(args[0].full_url)
            return Response()
        with mock.patch.object(mv, "urlopen", side_effect=fake_open):
            self.assertEqual(mv.cover_url_status("https://cover.example/a.jpg"), "verified")
            self.assertEqual(mv.cover_url_status("https://cover.example/a.jpg"), "verified")
        self.assertEqual(calls, ["https://cover.example/a.jpg"])

    def test_rejects_mismatched_names(self) -> None:
        hits = [_hit("完全不同的歌", "另一个艺人")]
        self.assertIsNone(self._verify("夜曲", "周杰伦", {"netease": lambda title, artist: hits}))

    def test_similar_track_but_wrong_artist_is_rejected(self) -> None:
        hits = [_hit("夜曲", "某个翻唱者")]
        self.assertIsNone(self._verify("夜曲", "周杰伦", {"netease": lambda title, artist: hits}))

    def test_high_confidence_hit_skips_other_sources(self) -> None:
        calls: list[str] = []

        def netease(title: str, artist: str) -> list[dict]:
            calls.append("netease")
            return [_hit("夜曲", "周杰伦", album="十一月的萧邦")]

        def qq(title: str, artist: str) -> list[dict]:
            calls.append("qq")
            return []

        result = self._verify("夜曲", "周杰伦", {"netease": netease, "qq": qq})

        self.assertIsNotNone(result)
        self.assertEqual(calls, ["netease"], "高置信命中后不应继续查询其他来源")

    def test_low_confidence_falls_back_to_next_source(self) -> None:
        def netease(title: str, artist: str) -> list[dict]:
            return [_hit("夜曲 现场版 特别收录", "周杰伦与朋友")]

        def qq(title: str, artist: str) -> list[dict]:
            return [_hit("夜曲", "周杰伦", album="十一月的萧邦", platform_id="QQ1")]

        result = self._verify("夜曲", "周杰伦", {"netease": netease, "qq": qq})

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "qq")
        self.assertEqual(result["platform_track_id"], "QQ1")

    def test_network_failure_is_treated_as_unverified(self) -> None:
        def boom(title: str, artist: str) -> list[dict]:
            raise TimeoutError("网络超时")

        self.assertIsNone(self._verify("夜曲", "周杰伦", {"netease": boom}))

    def test_blank_input_is_unverified(self) -> None:
        self.assertIsNone(self._verify("", "周杰伦", {"netease": lambda title, artist: []}))
        self.assertIsNone(self._verify("夜曲", "   ", {"netease": lambda title, artist: []}))


class VerifyManyTests(unittest.TestCase):
    def test_deduplicates_and_keeps_order(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake(title: str, artist: str) -> dict:
            calls.append((title, artist))
            return {"title": title, "artist": artist, "album": "", "cover": None,
                    "platform_track_id": "", "url": None, "source": "test", "score": 1.0}

        with mock.patch.object(mv, "verify_candidate", side_effect=fake):
            results = mv.verify_many([("A", "X"), ("B", "Y"), ("A", "X")])

        self.assertEqual(len(results), 3)
        self.assertEqual(calls, [("A", "X"), ("B", "Y")], "相同曲目只查询一次")
        self.assertEqual(results[0], results[2])

    def test_progress_callback_reports_totals(self) -> None:
        seen: list[tuple[int, int]] = []
        with mock.patch.object(mv, "verify_candidate", return_value=None):
            mv.verify_many([("A", "X"), ("B", "Y")], progress=lambda done, total: seen.append((done, total)))

        self.assertEqual(seen[-1], (2, 2))


if __name__ == "__main__":
    unittest.main()
