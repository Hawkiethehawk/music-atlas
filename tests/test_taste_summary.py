"""Scale-aware analysis tests: mode resolution, taste contracts and packet mapping."""

from __future__ import annotations

# 测试夹具曲目是合成数据，平台上不存在；关闭平台元数据核验，
# 核验逻辑本身由 tests/test_metadata_verify.py 与专门用例覆盖。
import os as _atlas_os
_atlas_os.environ.setdefault("ATLAS_METADATA_VERIFY", "off")

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from contracts import ContractError, validate_analysis_packet, validate_playlist_snapshot
from source_adapters import build_snapshot

ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = ROOT / "styles" / "style_taxonomy.json"

_spec = importlib.util.spec_from_file_location(
    "fake_taste_agent", ROOT / "tests" / "fixtures" / "fake_taste_agent.py"
)
fake_taste_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_taste_agent)

from analysis_contracts import validate_taste_summary_result  # noqa: E402
from taste_summary import (  # noqa: E402
    build_tracklist_stats,
    build_taste_prompt,
    map_taste_to_packet,
    resolve_analysis_mode,
    run_taste_analysis,
    summarize_review,
)

from musician_analyzer import apply_public_style_evidence, load_recommendation_policy, load_style_taxonomy  # noqa: E402


def _snapshot_with(count: int, tmp: Path, platform: str = "local") -> dict:
    tracks = [
        {
            "title": f"Track {index:03d}",
            "artist": f"Artist {index % 12}" if count > 12 else f"Artist {index}",
            "artists": [f"Artist {index % 12}" if count > 12 else f"Artist {index}"],
            "album": "Album",
            "song_url": f"https://music.apple.com/us/song/sample/{1000 + index}",
        }
        for index in range(count)
    ]
    payload = {"declared_track_count": count, "tracks": tracks}
    source = tmp / f"playlist-{count}.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return build_snapshot(
        source,
        reader_name="local_json",
        platform=platform,
        playlist_id=f"sample-{count}",
        playlist_name="规模测试歌单",
    )


class ResolveAnalysisModeTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(resolve_analysis_mode(0), "track_research")
        self.assertEqual(resolve_analysis_mode(200), "track_research")
        self.assertEqual(resolve_analysis_mode(201), "taste_summary")
        self.assertEqual(resolve_analysis_mode(999), "taste_summary")
        self.assertEqual(resolve_analysis_mode(1000), "artist_summary")
        self.assertEqual(resolve_analysis_mode(2000), "artist_summary")

    def test_invalid_count(self):
        with self.assertRaises(ContractError):
            resolve_analysis_mode(-1)
        with self.assertRaises(ContractError):
            resolve_analysis_mode(True)

    def test_thousand_song_packet_uses_only_sourced_artists(self):
        from taste_summary import build_sourced_summary_packet
        from contracts import require_analysis_coverage

        with tempfile.TemporaryDirectory() as tmp:
            snapshot = _snapshot_with(1000, Path(tmp))
            taxonomy = load_style_taxonomy(TAXONOMY_PATH)
            shell = build_sourced_summary_packet(snapshot, taxonomy, taxonomy_path=TAXONOMY_PATH)
            self.assertEqual(shell["analysis_mode"], "artist_summary")
            self.assertEqual(shell["style_analysis"]["classified_track_count"], 0)
            self.assertNotIn("axis_definitions", shell["style_analysis"])
            ref = taxonomy["known_style_refs"][0]
            records = []
            for track in shell["favorite_tracks"]:
                artist = track["artist"]
                records.append({"track_key": track["track_key"], "evidence": [
                    {"scope": "artist", "subject": {"artist": artist}, "source": "lastfm",
                     "status": "supported", "identity_status": "request_only",
                     "url": "https://www.last.fm/music/" + artist.replace(" ", "+"),
                     "retrieved_at": "2026-09-24T00:00:00Z", "tags": [{"tag": "Rock", "style_ref": ref}]},
                    {"scope": "track", "subject": {"artist": artist, "track": track["title"]},
                     "source": "lastfm", "status": "supported", "identity_status": "request_only",
                     "url": "https://www.last.fm/music/test/_/song", "retrieved_at": "2026-09-24T00:00:00Z",
                     "tags": [{"tag": "Rock", "style_ref": ref}]},
                ]})
            sourced = apply_public_style_evidence(shell, {"provider": "lastfm", "axis_policy": "removed",
                                                          "records": records})
            validate_analysis_packet(sourced)
            require_analysis_coverage(sourced)
            coverage = sourced["style_analysis"]["source_coverage"]
            self.assertEqual(coverage["weighted_artist_track_count"], 1000)
            self.assertEqual(coverage["track_evidence_count"], 0)
            self.assertEqual(sourced["style_analysis"]["classified_track_count"], 0)
            self.assertEqual(len(sourced["style_analysis"]["artist_style_distribution"]), 1)
            from musician_analyzer import write_coverage_report
            report = write_coverage_report(sourced, Path(tmp) / "coverage.json")
            self.assertEqual(report["quality_metric"], "min_artist_weight_share")
            self.assertEqual(report["review_queue"], [])

    def test_original_playlist_size_controls_artist_mode_after_filtering(self):
        from taste_summary import build_sourced_summary_packet

        with tempfile.TemporaryDirectory() as tmp:
            # 1917 original entries, 959 retained for analysis: the threshold
            # belongs to the source playlist, never the filtered work set.
            snapshot = _snapshot_with(959, Path(tmp))
            taxonomy = load_style_taxonomy(TAXONOMY_PATH)
            packet = build_sourced_summary_packet(
                snapshot, taxonomy, taxonomy_path=TAXONOMY_PATH,
                source_playlist_track_count=1917,
            )
            self.assertEqual(packet["source_track_count"], 959)
            self.assertEqual(packet["source_playlist_track_count"], 1917)
            self.assertEqual(packet["analysis_mode"], "artist_summary")
            self.assertEqual(packet["style_analysis"]["classified_track_count"], 0)
            self.assertEqual(packet["recommendation_policy"]["analysis_quality"],
                             {"min_artist_weight_share": 0.3})
            validate_analysis_packet(packet)
            self.assertEqual(apply_public_style_evidence(packet, packet["source_tags"])["analysis_mode"],
                             "artist_summary")
            for invalid in (True, 958, 0, 1917.0, "1917"):
                broken = json.loads(json.dumps(packet))
                broken["source_playlist_track_count"] = invalid
                with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                    validate_analysis_packet(broken)
                with self.subTest(apply_invalid=invalid), self.assertRaises(ContractError):
                    apply_public_style_evidence(broken, broken["source_tags"])
            legacy = json.loads(json.dumps(packet))
            legacy.pop("source_playlist_track_count")
            with self.assertRaises(ContractError):
                validate_analysis_packet(legacy)

    def test_verify_concurrency_scales_with_size(self):
        from web_workflow import analysis_verify_concurrency

        self.assertEqual(analysis_verify_concurrency(10), 12)
        self.assertEqual(analysis_verify_concurrency(50), 12)
        self.assertEqual(analysis_verify_concurrency(51), 20)
        self.assertEqual(analysis_verify_concurrency(2000), 20)


class TracklistStatsTest(unittest.TestCase):
    def test_duplicates_and_artist_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tracks = [
                {"title": "Same Song", "artist": "A", "artists": ["A"], "album": "x",
                 "song_url": f"https://music.apple.com/us/song/x/{1000 + index}"}
                for index in range(2)
            ]
            tracks.append({"title": "Other", "artist": "B", "artists": ["B", "C"], "album": "x",
                           "song_url": "https://music.apple.com/us/song/x/2000"})
            payload = {"declared_track_count": len(tracks), "tracks": tracks}
            source = tmp_path / "playlist.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            snapshot = build_snapshot(source, reader_name="local_json", platform="local",
                                      playlist_id="s", playlist_name="s")
            stats = build_tracklist_stats(snapshot)
            self.assertEqual(stats["total_rows"], 3)
            self.assertEqual(stats["unique_tracks"], 2)
            self.assertEqual(len(stats["duplicate_rows"]), 1)
            self.assertEqual(stats["duplicate_rows"][0]["count"], 2)
            self.assertEqual(stats["primary_counter"]["A"], 2)
            self.assertEqual(stats["credited_counter"]["C"], 1)

    def test_taste_prompt_prioritizes_primary_artists_not_guests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracks = []
            for index, artist in enumerate(["Alpha"] * 20 + ["Beta"] * 12 + ["Gamma"] * 4):
                tracks.append({
                    "title": f"Song {index:03d}", "artist": artist,
                    "artists": [artist, "Guest"], "album": "Album",
                    "song_url": f"https://music.apple.com/us/song/sample/{1000 + index}",
                })
            source = root / "playlist.json"
            source.write_text(json.dumps({"declared_track_count": len(tracks), "tracks": tracks}),
                              encoding="utf-8")
            snapshot = build_snapshot(source, reader_name="local_json", platform="local",
                                      playlist_id="s", playlist_name="s")
            stats = build_tracklist_stats(snapshot)
            from taste_summary import _request_id
            prompt = build_taste_prompt("taste_summary", snapshot, load_style_taxonomy(TAXONOMY_PATH),
                                        stats, request_id=_request_id(snapshot, "taste_summary"))
            self.assertIn("1. Alpha: 主艺人 20 首\n2. Beta: 主艺人 12 首\n3. Gamma: 主艺人 4 首", prompt)
            self.assertNotIn("Guest: 主艺人", prompt)
            self.assertIn("不能为了覆盖率编造来源", prompt)
            self.assertIn("歌名:Song 035;歌手:Gamma/Guest", prompt,
                          "研究优先序不能取代完整原始曲目清单")

    def test_large_taste_prompt_bounds_titles_without_changing_full_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            taxonomy = load_style_taxonomy(TAXONOMY_PATH)
            from taste_summary import _request_id
            for count, expected in ((500, 500), (501, 300), (1917, 300)):
                snapshot = _snapshot_with(count, root)
                stats = build_tracklist_stats(snapshot)
                prompt = build_taste_prompt("taste_summary", snapshot, taxonomy, stats,
                                            request_id=_request_id(snapshot, "taste_summary"))
                listed = [line for line in prompt.splitlines() if line.startswith("歌名:Track ")]
                self.assertEqual(len(listed), expected)
                self.assertEqual(len(set(listed)), expected)
                self.assertEqual(stats["total_rows"], count)
                self.assertIn(f'"total_rows": {count}', prompt)
                if count > 500:
                    self.assertIn(f"{expected}/{count} 首代表性曲目", prompt)
                    positions = [int(line.split(";", 1)[0].split(" ")[1]) for line in listed]
                    self.assertEqual(positions, sorted(positions))
                    self.assertLess(positions[0], count // 4)
                    self.assertGreater(positions[-1], count * 3 // 4)
                    self.assertIn("主艺人研究优先序", prompt)
                else:
                    self.assertIn("以下为完整歌名清单", prompt)

    def test_large_taste_prompt_preserves_titles_of_high_frequency_primary_artist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _snapshot_with(1917, root)
            for index, track in enumerate(snapshot["tracks"]):
                if index < 600:
                    track["artist"] = "Anchor"
                    track["artists"] = ["Anchor"]
            stats = build_tracklist_stats(snapshot)
            from taste_summary import _representative_tracklist_lines
            lines = _representative_tracklist_lines(snapshot, stats)
            self.assertEqual(len(lines), 300)
            self.assertGreaterEqual(sum("歌手:Anchor" in line for line in lines), 6)
            self.assertEqual(len(snapshot["tracks"]), 1917)


class TasteContractTest(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_style_taxonomy(TAXONOMY_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            self.snapshot = _snapshot_with(35, Path(tmp))
        self.stats = build_tracklist_stats(self.snapshot)

    def _prompt(self, mode="taste_summary"):
        from taste_summary import _request_id
        request_id = _request_id(self.snapshot, mode)
        return build_taste_prompt(mode, self.snapshot, self.taxonomy, self.stats,
                                  request_id=request_id), request_id

    def _valid_bundle(self, mode="taste_summary"):
        prompt, _ = self._prompt(mode)
        return fake_taste_agent.compose(prompt)

    def test_valid_bundle_passes(self):
        for mode in ("taste_summary", "artist_summary"):
            with self.subTest(mode=mode):
                prompt, _ = self._prompt(mode)
                bundle = fake_taste_agent.compose(prompt)
                validated = validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                                          taxonomy=self.taxonomy, mode=mode)
                self.assertEqual(validated["analysis_mode"], mode)

    def test_rejects_outside_artist(self):
        bundle = self._valid_bundle()
        bundle["artist_clusters"][0]["artist"] = "不存在的艺人"
        with self.assertRaises(ContractError):
            validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                          taxonomy=self.taxonomy, mode="taste_summary")

    def test_rejects_unknown_style_ref(self):
        bundle = self._valid_bundle()
        bundle["style_tags"][0]["tag"] = "style:not.in.taxonomy"
        with self.assertRaises(ContractError):
            validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                          taxonomy=self.taxonomy, mode="taste_summary")

    def test_rejects_humor_without_speculation(self):
        bundle = self._valid_bundle()
        bundle["editorial_review"]["humor_notes"] = [{"note": "笑话", "speculation": False}]
        with self.assertRaises(ContractError):
            validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                          taxonomy=self.taxonomy, mode="taste_summary")

    def test_rejects_missing_themes_in_taste_mode(self):
        bundle = self._valid_bundle()
        bundle.pop("semantic_themes")
        with self.assertRaises(ContractError):
            validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                          taxonomy=self.taxonomy, mode="taste_summary")

    def test_rejects_themes_in_artist_mode(self):
        prompt, _ = self._prompt("artist_summary")
        bundle = fake_taste_agent.compose(prompt)
        bundle["semantic_themes"] = [
            {"theme": "越界主题", "tracks": ["A", "B", "C"], "note": "artist 模式不应携带主题"}
        ]
        with self.assertRaises(ContractError):
            validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                          taxonomy=self.taxonomy, mode="artist_summary")

    def test_rejects_recommendation_fields(self):
        bundle = self._valid_bundle()
        bundle["recommendations"] = [{"title": "不存在的歌"}]
        with self.assertRaises(ContractError):
            validate_taste_summary_result(bundle, snapshot=self.snapshot,
                                          taxonomy=self.taxonomy, mode="taste_summary")


class RunTasteAnalysisTest(unittest.TestCase):
    def _run(self, snapshot, directory, *, command="python tests/fixtures/fake_taste_agent.py"):
        # 与真实链路同契约：stdin 提示词 -> stdout 单个 JSON 对象。
        return run_taste_analysis(
            snapshot, TAXONOMY_PATH, directory, command=command, timeout=120,
            execute=lambda cmd, prompt, timeout: fake_taste_agent.compose(prompt),
        )

    def test_invalid_style_cluster_stays_unclassified_without_fallback(self):
        from contracts import write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, _snapshot_with(210, root))

            def execute(_command, prompt, timeout):
                result = fake_taste_agent.compose(prompt)
                result["artist_clusters"][0]["style_refs"] = ["style:not.in.taxonomy"]
                return result

            packet, _ = run_taste_analysis(snapshot_path, TAXONOMY_PATH, root / "run",
                                            command="fake", timeout=120, execute=execute)
            first = packet["track_style_assignments"][0]
            self.assertEqual(first["classification_status"], "unclassified")
            self.assertEqual(first["style_refs"], [])
            self.assertEqual(first["sources"], [])
            self.assertEqual(packet["track_style_assignments"][1]["classification_status"],
                             "unclassified", "Agent 的 reference_url 不能代替本次实际检索")

    def test_all_invalid_style_clusters_retry_without_invented_classification(self):
        from contracts import write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, _snapshot_with(210, root))
            attempts = []

            def execute(_command, prompt, timeout):
                attempts.append(1)
                result = fake_taste_agent.compose(prompt)
                for cluster in result["artist_clusters"]:
                    cluster["style_refs"] = ["style:not.in.taxonomy"]
                return result

            with self.assertRaisesRegex(ContractError, "连续 3 次未通过契约校验"):
                run_taste_analysis(snapshot_path, TAXONOMY_PATH, root / "run",
                                   command="fake", timeout=120, execute=execute)
            self.assertEqual(len(attempts), 3)
            self.assertFalse((root / "run" / "taste_summary.json").exists())

    def test_fake_agent_utf8_stdio(self):
        """子进程链路：PYTHONIOENCODING=utf-8 时 stdin/stdout 均为 UTF-8（同真实执行器契约）。"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = _snapshot_with(35, Path(tmp))
            stats = build_tracklist_stats(snapshot)
            taxonomy = load_style_taxonomy(TAXONOMY_PATH)
            from taste_summary import _request_id
            prompt = build_taste_prompt("taste_summary", snapshot, taxonomy, stats,
                                        request_id=_request_id(snapshot, "taste_summary"))
            proc = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "fixtures" / "fake_taste_agent.py")],
                input=prompt, capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}, check=True,
            )
            bundle = json.loads(proc.stdout)
            self.assertEqual(bundle["bundle_type"], "taste_summary_result")

    def test_end_to_end_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = _snapshot_with(210, tmp_path)
            snapshot_path = tmp_path / "snapshot.json"
            from contracts import write_json
            write_json(snapshot_path, snapshot)
            snapshot = validate_playlist_snapshot(read := __import__("contracts").read_json(snapshot_path))
            with tempfile.TemporaryDirectory() as run_tmp:
                packet, bundle_path = self._run(snapshot_path, Path(run_tmp))
                self.assertTrue(bundle_path.is_file())
                self.assertEqual(packet["packet_type"], "musician_analysis")
                self.assertEqual(packet["analysis_mode"], "taste_summary")
                validate_analysis_packet(packet)
                # 逐曲身份与 favorite_track_keys 顺序一致，但旧 Agent 摘要不构成来源。
                self.assertEqual(
                    [item["track_key"] for item in packet["track_style_assignments"]],
                    packet["favorite_track_keys"],
                )
                self.assertTrue(
                    all(item["applied_scope"] == "unknown"
                        for item in packet["track_style_assignments"])
                )
                self.assertEqual(packet["style_analysis"]["profile_catalog_mode"], "taste_summary")
                self.assertEqual(packet["recommendation_policy"]["analysis_quality"]["min_track_or_album_share"], 0.5)
                self.assertEqual(packet["style_analysis"]["classified_track_count"], 0)
                self.assertNotIn("style_axes", packet["style_analysis"])
                self.assertNotIn("musician_relation",
                                 [item["candidate_type"] for item in packet["recommendation_policy"]["recall_mix"]])
                self.assertIn("editorial_review", packet["taste_summary"])
                review = summarize_review(packet["taste_summary"])
                self.assertIn("headline", review)
                # 复用缓存：第二次运行不重新执行。
                packet2, _ = self._run(snapshot_path, Path(run_tmp))
                self.assertEqual(packet2["taste_summary"]["request_id"],
                                 packet["taste_summary"]["request_id"])

    def test_track_research_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = _snapshot_with(10, Path(tmp))
            snapshot_path = Path(tmp) / "snapshot.json"
            from contracts import write_json
            write_json(snapshot_path, snapshot)
            with tempfile.TemporaryDirectory() as run_tmp:
                with self.assertRaises(ContractError):
                    self._run(snapshot_path, Path(run_tmp))

    def test_outside_semantic_track_is_removed_before_validation(self):
        from contracts import write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, _snapshot_with(210, root))
            attempts = []

            def execute(_command, prompt, timeout):
                attempts.append(1)
                result = fake_taste_agent.compose(prompt)
                result["semantic_themes"][0]["tracks"] = [
                    "Track 000", "捏造的歌名", "Track 001", "Track 002",
                ]
                return result

            packet, _ = run_taste_analysis(
                snapshot_path, TAXONOMY_PATH, root / "run", command="fake", timeout=120,
                execute=execute,
            )
            themes = packet["taste_summary"]["semantic_themes"]
            self.assertEqual(attempts, [1], "可校正的单首假歌不应触发重试")
            self.assertEqual(themes[0]["tracks"], ["Track 000", "Track 001", "Track 002"])
            self.assertEqual(len(themes), 2)

    def test_theme_with_fewer_than_three_real_tracks_is_dropped(self):
        from contracts import write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, _snapshot_with(210, root))

            def execute(_command, prompt, timeout):
                result = fake_taste_agent.compose(prompt)
                result["semantic_themes"][0]["tracks"] = [
                    "Track 000", "Track 001", "捏造的歌名",
                ]
                result["semantic_themes"].append({
                    "theme": "TEST_ONLY 第三个真实主题",
                    "tracks": ["Track 006", "Track 007", "Track 008"],
                    "note": "三个歌名均在当前快照。",
                })
                return result

            packet, _ = run_taste_analysis(
                snapshot_path, TAXONOMY_PATH, root / "run", command="fake", timeout=120,
                execute=execute,
            )
            themes = packet["taste_summary"]["semantic_themes"]
            self.assertEqual(len(themes), 2)
            self.assertNotIn("TEST_ONLY 主题一", [theme["theme"] for theme in themes])
            self.assertEqual(themes[-1]["tracks"], ["Track 006", "Track 007", "Track 008"])

    def test_only_one_supported_theme_still_retries_and_fails(self):
        from contracts import write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, _snapshot_with(210, root))
            attempts = []

            def execute(_command, prompt, timeout):
                attempts.append(1)
                result = fake_taste_agent.compose(prompt)
                result["semantic_themes"][0]["tracks"] = [
                    "Track 000", "Track 001", "捏造的歌名",
                ]
                return result

            with self.assertRaisesRegex(ContractError, "连续 3 次未通过契约校验"):
                run_taste_analysis(
                    snapshot_path, TAXONOMY_PATH, root / "run", command="fake", timeout=120,
                    execute=execute,
                )
            self.assertEqual(len(attempts), 3)
            self.assertFalse((root / "run" / "taste_summary.json").exists())

    def test_fully_invented_semantic_themes_still_rejected(self):
        from contracts import write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            write_json(snapshot_path, _snapshot_with(210, root))
            attempts = []

            def execute(_command, prompt, timeout):
                attempts.append(1)
                result = fake_taste_agent.compose(prompt)
                for index, theme in enumerate(result["semantic_themes"]):
                    theme["tracks"] = [f"编造曲目 {index}-{number}" for number in range(3)]
                return result

            with self.assertRaisesRegex(ContractError, "连续 3 次未通过契约校验"):
                run_taste_analysis(
                    snapshot_path, TAXONOMY_PATH, root / "run", command="fake", timeout=120,
                    execute=execute,
                )
            self.assertEqual(len(attempts), 3)
            self.assertFalse((root / "run" / "taste_summary.json").exists())


class WebWorkflowTasteModeTest(unittest.TestCase):
    """网页流程不会把没有公开候选事实的本地曲目发布为推荐。"""

    def test_local_tracks_without_public_candidates_are_not_published(self):
        import os
        from contracts import read_json
        from web_workflow import build_parser, run_web_workflow

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            playlist = tmp_path / "playlist.json"
            tracks = [
                {"title": f"Song {index:03d}", "artist": f"Artist {index % 9}",
                 "artists": [f"Artist {index % 9}"], "album": "Album",
                 "song_url": f"https://music.apple.com/us/song/s/{2000 + index}"}
                for index in range(35)
            ]
            playlist.write_text(json.dumps({"declared_track_count": 35, "tracks": tracks},
                                           ensure_ascii=False), encoding="utf-8")
            runtime_dir = tmp_path / "run"
            current_data = tmp_path / "current.json"
            args = build_parser().parse_args([
                "--runtime-dir", str(runtime_dir),
                "--current-data", str(current_data),
                "--source-kind", "local_json",
                "--input", str(playlist),
                "--platform", "local",
                "--analysis-command", f'{sys.executable} tests/fixtures/fake_taste_agent.py',
                "--recommendation-command", f'{sys.executable} tests/fixtures/fake_agent.py',
                "--analysis-timeout", "120", "--recommendation-timeout", "120",
            ])
            cwd = os.getcwd()
            os.chdir(ROOT)
            try:
                from unittest.mock import patch
                with patch("agent_lastfm.analyze"), patch("lastfm_pipeline.LastFM"), patch("lastfm_pipeline.validate_knowledge"), patch("lastfm_pipeline.collect_tags", return_value={"records":[]}), patch("lastfm_pipeline.discover", return_value=([],{})), self.assertRaises(ContractError):
                    run_web_workflow(args)
            finally:
                os.chdir(cwd)
            self.assertFalse(current_data.exists())


class WebWorkflowScaleRouteTest(unittest.TestCase):
    """规模路由：201–999 首按实际公开来源汇总，无资料不能发布。"""

    def test_210_tracks_use_taste_summary_and_skip_track_facts(self):
        import os
        from unittest.mock import patch
        from contracts import read_json
        from web_workflow import build_parser, run_web_workflow

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            playlist = tmp_path / "playlist.json"
            tracks = [
                {"title": f"Song {index:03d}", "artist": f"Artist {index % 9}",
                 "artists": [f"Artist {index % 9}"], "album": "Album",
                 "song_url": f"https://music.apple.com/us/song/s/{3000 + index}"}
                for index in range(210)
            ]
            playlist.write_text(json.dumps({"declared_track_count": 210, "tracks": tracks},
                                           ensure_ascii=False), encoding="utf-8")
            runtime_dir = tmp_path / "run"
            args = build_parser().parse_args([
                "--runtime-dir", str(runtime_dir),
                "--current-data", str(tmp_path / "current.json"),
                "--source-kind", "local_json",
                "--input", str(playlist),
                "--platform", "local",
                "--analysis-command", f'{sys.executable} tests/fixtures/fake_taste_agent.py',
                "--recommendation-command", f'{sys.executable} tests/fixtures/fake_agent.py',
                "--analysis-timeout", "120", "--recommendation-timeout", "120",
            ])
            cwd = os.getcwd()
            os.chdir(ROOT)
            try:
                with patch("relationship_sources.collect_relationships", return_value={}), \
                     patch("lastfm_pipeline.LastFM"), \
                     patch("lastfm_pipeline.discover", return_value=([], {})), \
                     patch("metadata_verify.verify_many", return_value=[]):
                    # 合成曲目没有公开来源，必须在分析质量门槛前失败。
                    with self.assertRaises(ContractError):
                        run_web_workflow(args)
            finally:
                os.chdir(cwd)

            self.assertFalse((runtime_dir / "musician_analysis.json").exists(),
                             "没有实际来源时不可发布分析产物")
            self.assertFalse((tmp_path / "current.json").exists())
            self.assertFalse((runtime_dir / "track_facts.json").exists(),
                             "摘要模式应跳过逐曲事实核验")


if __name__ == "__main__":
    unittest.main()
