from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import ContractError, validate_recommendation_bundle
from evidence import (
    check_evidence_acceptance,
    claim_required_grade,
    classify_source,
    extract_source_identifier,
    suggest_evidence_grade,
    verify_and_check_items,
    verify_evidence_item,
)
from recommender import rank_bundle
from test_workflow import candidate_fixture, minimal_analysis_packet


def evidence_item(claim_type: str, url: str, *, claim: str = "事实") -> dict:
    return {"claim_type": claim_type, "claim": claim, "url": url}


class EvidenceQualityTests(unittest.TestCase):
    def test_urls_are_classified_into_source_classes(self) -> None:
        cases = {
            "https://musicbrainz.org/recording/11111111-1111-1111-1111-111111111111": "musicbrainz",
            "https://www.wikidata.org/wiki/Q12345": "wikidata",
            "https://www.last.fm/music/Band": "lastfm",
            "https://listenbrainz.org/recording/x": "listenbrainz",
            "https://band.bandcamp.com/track/song": "bandcamp",
            "https://www.youtube.com/watch?v=abcdefghijk": "youtube",
            "https://open.spotify.com/track/0123456789012345678901": "spotify",
            "https://music.apple.com/us/song/x": "official",
            "https://example.com/artist": "other",
        }
        for url, expected in cases.items():
            self.assertEqual(classify_source(url), expected, url)

    def test_stable_public_identifiers_are_extracted(self) -> None:
        musicbrainz_uuid = "9e2f39ad-5a54-47f1-a0c8-4a8e2f0b2f3a"
        self.assertEqual(
            extract_source_identifier(f"https://musicbrainz.org/recording/{musicbrainz_uuid}"),
            f"musicbrainz:{musicbrainz_uuid}",
        )
        self.assertEqual(
            extract_source_identifier("https://www.wikidata.org/wiki/Q315"),
            "wikidata:Q315",
        )
        self.assertEqual(
            extract_source_identifier("https://www.youtube.com/watch?v=abcdefghijk"),
            "youtube:abcdefghijk",
        )
        self.assertEqual(
            extract_source_identifier("https://open.spotify.com/track/0123456789012345678901"),
            "spotify:0123456789012345678901",
        )
        self.assertIsNone(extract_source_identifier("https://example.com/artist"))

    def test_evidence_item_gets_provenance_and_verification_result(self) -> None:
        item = evidence_item(
            "track_identity",
            "https://musicbrainz.org/recording/11111111-1111-1111-1111-111111111111",
        )
        verified = verify_evidence_item(item)
        self.assertEqual(verified["source_class"], "musicbrainz")
        self.assertEqual(verified["source_identifier"], "musicbrainz:11111111-1111-1111-1111-111111111111")
        self.assertEqual(verified["verification_result"], "verified")

    def test_duplicated_and_unverifiable_evidence_are_flagged(self) -> None:
        url = "https://example.com/page"
        seen: set[str] = set()
        first = verify_evidence_item(evidence_item("style", url), seen_urls=seen)
        second = verify_evidence_item(evidence_item("style", url), seen_urls=seen)
        self.assertEqual(first["verification_result"], "unverified")
        self.assertEqual(second["verification_result"], "duplicated")

    def test_explicit_external_verdicts_are_preserved(self) -> None:
        # 外部适配器显式给出的 contradictory / inaccessible 判定被保留，不被离线回退覆盖
        contradictory = evidence_item(
            "release",
            "https://www.wikidata.org/wiki/Q315",
            claim="同一专辑的发行日期与另一来源矛盾",
        )
        contradictory["verification_result"] = "contradictory"
        inaccessible = evidence_item("style", "https://band.bandcamp.com/track/song")
        inaccessible["verification_result"] = "inaccessible"
        verified_contradictory = verify_evidence_item(contradictory)
        verified_inaccessible = verify_evidence_item(inaccessible)
        self.assertEqual(verified_contradictory["verification_result"], "contradictory")
        self.assertEqual(verified_inaccessible["verification_result"], "inaccessible")

    def test_explicit_verified_is_kept_and_unknown_result_is_ignored(self) -> None:
        item = evidence_item("track_identity", "https://example.com/x")
        item["verification_result"] = "verified"  # 外部在线适配器已核验
        verified = verify_evidence_item(item)
        self.assertEqual(verified["verification_result"], "verified")
        item["verification_result"] = "made-up"
        verified = verify_evidence_item(item)
        self.assertEqual(verified["verification_result"], "unverified")

    def test_stale_evidence_is_flagged_by_retrieval_time(self) -> None:
        item = evidence_item("release", "https://musicbrainz.org/release/11111111-1111-1111-1111-111111111111")
        item["retrieved_at"] = "2001-01-01T00:00:00Z"
        verified = verify_evidence_item(item)
        self.assertEqual(verified["verification_result"], "stale")
        # 检索时间新且是稳定标识符 -> verified
        item["retrieved_at"] = None
        verified = verify_evidence_item(item)
        self.assertEqual(verified["verification_result"], "verified")

    def test_contradictory_inaccessible_are_valid_contract_statuses(self) -> None:
        from contracts import EVIDENCE_VERIFICATION_STATUSES

        self.assertLessEqual(
            {"stale", "contradictory", "inaccessible", "duplicated"},
            EVIDENCE_VERIFICATION_STATUSES,
        )

    def test_grade_rules_require_stronger_evidence_for_identity_claims(self) -> None:
        musicbrainz_raw = evidence_item("track_identity", "https://musicbrainz.org/recording/11111111-1111-1111-1111-111111111111")
        example_raw = evidence_item("track_identity", "https://example.com/x")
        musicbrainz = verify_evidence_item(musicbrainz_raw)
        example = verify_evidence_item(example_raw)
        self.assertEqual(musicbrainz["source_class"], "musicbrainz")
        self.assertEqual(example["source_class"], "other")
        self.assertEqual(claim_required_grade("track_identity", [musicbrainz]), "A")
        self.assertEqual(claim_required_grade("track_identity", [example]), "C")
        self.assertEqual(suggest_evidence_grade([musicbrainz]), "A")
        self.assertEqual(suggest_evidence_grade([example]), "C")
        # 来源支持 A 时，保守声明 C 可接受；不会虚报
        verdict_conservative = check_evidence_acceptance([musicbrainz], "C")
        self.assertTrue(verdict_conservative["accepted"])
        # 来源只支持 C 时，声明 A 属于虚报，被拒绝
        verdict_overclaimed = check_evidence_acceptance([example], "A")
        self.assertFalse(verdict_overclaimed["accepted"])
        self.assertGreaterEqual(len(verdict_overclaimed["violating_items"]), 1)

    def test_evidence_fields_are_optional_in_the_bundle_contract(self) -> None:
        packet = minimal_analysis_packet()
        candidates = [
            candidate_fixture(packet, index, candidate_type)
            for index, candidate_type in enumerate(
                ("artist_continuation", "musician_relation", "style_neighbor", "exploration"),
                1,
            )
        ]
        # 不带出处字段的旧候选仍然合法（出处字段是可选的输入契约）
        bundle = {
            "schema_version": "2.0",
            "bundle_type": "recommendation_bundle",
            "bundle_stage": "candidate_pool",
            "status": "ready",
            "analysis_id": "analysis-test",
            "generated_at": "2026-01-01T00:00:00Z",
            "candidate_pool": candidates,
            "recommendations": [],
        }
        validate_recommendation_bundle(bundle, packet)
        ranked = rank_bundle(bundle, packet)
        validate_recommendation_bundle(ranked, packet)
        # 带无效 verification_result 的候选被拒绝
        candidate = candidates[0]
        candidate["evidence_items"][0]["verification_result"] = "made-up"
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(bundle, packet)


if __name__ == "__main__":
    unittest.main()
