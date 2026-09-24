"""Step 2/3 guards for the complete, deterministic Atlas workflow."""

from __future__ import annotations

import threading
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from contracts import ContractError, track_key
from lastfm_pipeline import select
from web_workflow import _select_review_groups_fast, build_parser, run_web_workflow


FIXTURE_PLAYLIST = Path(__file__).resolve().parent / "fixtures" / "playlist_sample.json"
MIX = (("style_neighbor", 4), ("artist_continuation", 3),
       ("musician_relation", 2), ("exploration", 1))


def workflow_args(root: Path):
    return build_parser().parse_args([
        "--runtime-dir", str(root / "job"),
        "--current-data", str(root / "current.json"),
        "--source-kind", "local_json", "--input", str(FIXTURE_PLAYLIST),
        "--platform", "apple_music",
    ])


class StepTwoPublicationGateTests(unittest.TestCase):
    def test_step_two_failure_never_starts_recommendation_or_publishes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            with mock.patch("web_workflow.emit", side_effect=lambda event, **data: events.append((event, data))), \
                 mock.patch("web_workflow.analyze_and_validate", side_effect=ContractError("Step 2 invalid")), \
                 mock.patch("web_workflow._discover_unique_candidates", side_effect=AssertionError("Step 3 crossed the gate")):
                with self.assertRaisesRegex(ContractError, "Step 2 invalid"):
                    run_web_workflow(workflow_args(root))

            self.assertFalse(any(item.get("stage") == "recommendation" for _, item in events))
            self.assertFalse((root / "current.json").exists())
            self.assertFalse((root / "job" / "web_job_report.json").exists())

    def test_step_two_failure_cannot_leak_a_preliminary_preview(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            args = workflow_args(root)
            # Even if an older Web caller still passes --preview, an incomplete
            # analysis cannot publish a provisional song list or ready event.
            args.preview = True
            preview_event = threading.Event()
            events = []

            def capture(event, **data):
                events.append((event, data))
                if event == "preview_ready":
                    preview_event.set()

            def reject_analysis(*_args, **_kwargs):
                preview_event.wait(0.5)
                raise ContractError("Step 2 invalid")

            with mock.patch("web_workflow.emit", side_effect=capture), \
                 mock.patch("web_workflow.analyze_and_validate", side_effect=reject_analysis), \
                 mock.patch("preview.preview_recommendations", return_value=[
                     {"title": "Provisional", "artist": "Other", "url": "https://music.example/song"},
                 ]), \
                 mock.patch("web_workflow._discover_unique_candidates", side_effect=AssertionError("Step 3 crossed the gate")):
                with self.assertRaisesRegex(ContractError, "Step 2 invalid"):
                    run_web_workflow(args)

            self.assertNotIn("preview_ready", [event for event, _ in events])
            self.assertFalse((root / "job" / "web_preview.json").exists())
            self.assertFalse((root / "current.json").exists())


class FinalSongSelectionTests(unittest.TestCase):
    def test_program_selects_three_4_3_2_1_groups_with_30_unique_tracks(self):
        packet = {
            "analysis_id": "fast-selection-fixture",
            "selection_mode": "lastfm_constraints_v1",
            "strict_recall_mix": True,
            "playlist_exclusion": {"track_keys": [track_key("Already Saved", "Seed")],
                                   "platform_track_ids": ["saved"]},
            "recommendation_policy": {
                "target_recommendations": 10, "max_per_artist": 1, "max_per_project": 1,
                "recall_mix": [{"candidate_type": kind, "target_ratio": number / 10}
                               for kind, number in MIX],
            },
            "agent_islands": [{"id": f"island-{index}", "record_ids": []} for index in range(3)],
            "favorite_tracks": [{"title": "Already Saved", "artist": "Seed"}],
        }
        candidates = []
        for kind, per_group in MIX:
            for index in range(per_group * 3):
                number = len(candidates)
                candidates.append({
                    "canonical_track_id": f"platform:netease:unique-{number}",
                    "platform_track_id": f"unique-{number}",
                    "title": f"Song {number}", "artist": f"Artist {number}",
                    "project": f"Album {number}", "candidate_type": kind,
                    "provider_similarity": {"seed": "Seed", "artist": f"Artist {number}"},
                    "provider_relation": ({"seed": "Seed", "person": "Person",
                                           "url": "https://musicbrainz.org/artist/fixture",
                                           "sources": ["https://musicbrainz.org/artist/fixture"]}
                                          if kind == "musician_relation" else {}),
                    "metadata_verified": {
                        "source": "netease", "title": f"Song {number}",
                        "artist": f"Artist {number}", "platform_track_id": f"unique-{number}",
                        "url": f"https://music.163.com/song?id=unique-{number}",
                    },
                    "sources": [f"https://music.163.com/song?id=unique-{number}"],
                })

        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}), \
             mock.patch("web_workflow.rank_bundle", side_effect=select), \
             mock.patch("agent_lastfm.invoke", side_effect=AssertionError("selection must not invoke Agent")), \
             mock.patch("web_workflow.emit"):
            prepared, groups, report = _select_review_groups_fast(
                packet, candidates, required=30, stage="recommendation",
            )

        self.assertEqual(len(prepared), 30)
        self.assertEqual(len(groups), 3)
        self.assertEqual(report["status"], "not_performed")
        self.assertEqual(report["selection_validation"], "passed")
        ids = []
        keys = []
        for group in groups:
            selected = group["recommendations"]
            self.assertEqual(len(selected), 10)
            self.assertEqual(Counter(row["candidate_type"] for row in selected), dict(MIX))
            ids.extend(row["canonical_track_id"] for row in selected)
            keys.extend(track_key(row["title"], row["artist"]) for row in selected)
        self.assertEqual(len(set(ids)), 30)
        self.assertEqual(len(set(keys)), 30)
        self.assertFalse(set(ids) & {"platform:netease:saved"})
        self.assertFalse(set(keys) & set(packet["playlist_exclusion"]["track_keys"]))

    def test_missing_relation_evidence_reallocates_without_forging_relation_rows(self):
        packet = {
            "analysis_id": "route-reallocation-fixture",
            "selection_mode": "lastfm_constraints_v1",
            "strict_recall_mix": True,
            "playlist_exclusion": {"track_keys": [], "platform_track_ids": []},
            "recommendation_policy": {
                "target_recommendations": 10, "max_per_artist": 1, "max_per_project": 1,
                "recall_mix": [{"candidate_type": kind, "target_ratio": number / 10}
                               for kind, number in MIX],
            },
            "agent_islands": [{"id": f"island-{index}", "record_ids": []} for index in range(3)],
            "favorite_tracks": [],
        }
        candidates = []
        for kind, count in (("style_neighbor", 15), ("artist_continuation", 12), ("exploration", 3)):
            for index in range(count):
                number = len(candidates)
                url = f"https://music.163.com/song?id=reallocated-{number}"
                candidates.append({
                    "canonical_track_id": f"platform:netease:reallocated-{number}",
                    "platform_track_id": f"reallocated-{number}",
                    "title": f"Reallocated {number}", "artist": f"Artist {number}",
                    "project": f"Album {number}", "candidate_type": kind,
                    "provider_similarity": {"seed": "Seed", "artist": f"Artist {number}"},
                    "metadata_verified": {
                        "source": "netease", "title": f"Reallocated {number}",
                        "artist": f"Artist {number}", "platform_track_id": f"reallocated-{number}",
                        "url": url,
                    },
                    "sources": [url],
                })

        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}), \
             mock.patch("web_workflow.rank_bundle", side_effect=select), \
             mock.patch("web_workflow.emit"):
            prepared, groups, report = _select_review_groups_fast(
                packet, candidates, required=30, stage="recommendation",
            )

        self.assertEqual(len(prepared), 30)
        self.assertEqual(len(groups), 3)
        self.assertEqual(report["route_reallocation"]["route_shortfall"], {"musician_relation": 6})
        self.assertEqual(report["route_reallocation"]["effective_quota_per_group"], {
            "style_neighbor": 5, "artist_continuation": 4,
            "musician_relation": 0, "exploration": 1,
        })
        for group in groups:
            self.assertEqual(Counter(row["candidate_type"] for row in group["recommendations"]), {
                "style_neighbor": 5, "artist_continuation": 4, "exploration": 1,
            })
            self.assertNotIn("musician_relation", [row["candidate_type"] for row in group["recommendations"]])

    def test_hard_gate_rejects_unverified_identity_and_playlist_overlap(self):
        from web_workflow import _validate_selection_hard_constraints

        packet = {"recommendation_policy": {"target_recommendations": 1},
                  "playlist_exclusion": {"track_keys": [], "platform_track_ids": []}}
        def song(index):
            url = f"https://music.163.com/song?id={index}"
            return {"canonical_track_id": f"platform:netease:{index}",
                    "platform_track_id": str(index), "title": f"Track {index}",
                    "artist": f"Artist {index}", "candidate_type": "style_neighbor",
                    "metadata_verified": {"source": "netease", "platform_track_id": str(index),
                                          "title": f"Track {index}", "artist": f"Artist {index}", "url": url},
                    "sources": [url]}
        groups = [{"recommendations": [song(index)]} for index in range(3)]
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}):
            _validate_selection_hard_constraints(packet, groups)
            groups[1]["recommendations"][0]["metadata_verified"]["artist"] = "Impostor"
            with self.assertRaisesRegex(ContractError, "身份与来源记录不一致"):
                _validate_selection_hard_constraints(packet, groups)
            groups[1]["recommendations"][0]["metadata_verified"]["artist"] = "Artist 1"
            packet["playlist_exclusion"]["track_keys"] = [track_key("Track 0", "Artist 0")]
            with self.assertRaisesRegex(ContractError, "原歌单曲目"):
                _validate_selection_hard_constraints(packet, groups)
            packet["playlist_exclusion"]["track_keys"] = []
            groups[1]["recommendations"][0] = song(0)
            with self.assertRaisesRegex(ContractError, "重复曲目"):
                _validate_selection_hard_constraints(packet, groups)


if __name__ == "__main__":
    unittest.main()
