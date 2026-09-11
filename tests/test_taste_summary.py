"""Scale-aware analysis tests: mode resolution, taste contracts and packet mapping."""

from __future__ import annotations

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

from musician_analyzer import load_recommendation_policy, load_style_taxonomy  # noqa: E402


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
        self.assertEqual(resolve_analysis_mode(30), "track_research")
        self.assertEqual(resolve_analysis_mode(31), "taste_summary")
        self.assertEqual(resolve_analysis_mode(500), "taste_summary")
        self.assertEqual(resolve_analysis_mode(501), "artist_summary")

    def test_invalid_count(self):
        with self.assertRaises(ContractError):
            resolve_analysis_mode(-1)
        with self.assertRaises(ContractError):
            resolve_analysis_mode(True)


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
            snapshot = _snapshot_with(35, tmp_path)
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
                # 逐曲分配与 favorite_track_keys 顺序一致，且来源于品味摘要。
                self.assertEqual(
                    [item["track_key"] for item in packet["track_style_assignments"]],
                    packet["favorite_track_keys"],
                )
                self.assertTrue(
                    all(item["applied_scope"] in {"taste_artist", "taste_unknown"}
                        for item in packet["track_style_assignments"])
                )
                self.assertEqual(packet["style_analysis"]["profile_catalog_mode"], "taste_summary")
                self.assertEqual(packet["recommendation_policy"]["analysis_quality"]["min_classified_share"], 0.3,
                                 "taste 模式覆盖门槛应为用户批准的 30%")
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


class WebWorkflowTasteModeTest(unittest.TestCase):
    """35 首歌单走完网页工作流全流程：品味摘要分析 → 推荐 → 发布。"""

    def test_full_run(self):
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
                code = run_web_workflow(args)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)

            packet = read_json(runtime_dir / "musician_analysis.json")
            self.assertEqual(packet["analysis_mode"], "taste_summary")
            self.assertTrue((runtime_dir / "analysis_research" / "taste_summary.json").is_file())
            report = read_json(runtime_dir / "web_job_report.json")
            self.assertEqual(report["status"], "completed")
            payload = read_json(current_data)
            self.assertEqual(payload["payload_type"], "music_atlas_web")
            self.assertEqual(len(payload["recommendations"]), 10)
            self.assertIsNotNone(payload.get("taste_review"), "品味摘要模式应输出锐评投影")
            self.assertIn("headline", payload["taste_review"])


class WorkflowCliScaleTest(unittest.TestCase):
    """workflow.py run/analyze 的规模分档：35 首走品味摘要，3 首走原逐曲路径。"""

    def _args(self, tmp_path, playlist, extra=()):
        from workflow import build_parser
        return build_parser().parse_args([
            "analyze",
            "--snapshot", str(playlist),
            "--output", str(tmp_path / "musician_analysis.json"),
            "--analysis-command", f'{sys.executable} tests/fixtures/fake_taste_agent.py',
            "--analysis-timeout", "120",
            *extra,
        ])

    def test_analyze_large_playlist_uses_taste_summary(self):
        import os
        from workflow import command_analyze

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = _snapshot_with(35, tmp_path)
            from contracts import write_json
            snapshot_path = tmp_path / "snapshot.json"
            write_json(snapshot_path, snapshot)
            args = self._args(tmp_path, snapshot_path)
            cwd = os.getcwd()
            os.chdir(ROOT)
            try:
                code = command_analyze(args)
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)
            packet = json.loads((tmp_path / "musician_analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(packet["analysis_mode"], "taste_summary")
            validate_analysis_packet(packet)
            self.assertIn("品味摘要", (tmp_path / "musician_analysis.md").read_text(encoding="utf-8"))

    def test_analyze_rejects_import_mode_for_large_playlist(self):
        from workflow import build_parser, command_analyze

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = _snapshot_with(35, tmp_path)
            from contracts import write_json
            snapshot_path = tmp_path / "snapshot.json"
            write_json(snapshot_path, snapshot)
            args = build_parser().parse_args([
                "analyze",
                "--snapshot", str(snapshot_path),
                "--output", str(tmp_path / "musician_analysis.json"),
                "--import-analysis-results",
            ])
            cwd = os.getcwd()
            os.chdir(ROOT)
            try:
                with self.assertRaises(ContractError):
                    command_analyze(args)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
