from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from contracts import ContractError, validate_playlist_snapshot
from source_adapters import build_snapshot
from web_workflow import (
    LIMIT_REQUEST_FILENAME,
    _apply_track_limit,
    _await_track_limit,
    _build_source,
    _read_limit_request,
    _resolve_netease_source,
    _validate_url,
    run_web_workflow,
)

FIXTURE_PLAYLIST = Path(__file__).resolve().parent / "fixtures" / "playlist_sample.json"
FIXTURE_TRACK_COUNT = 3


def fixture_snapshot() -> dict:
    return build_snapshot(
        FIXTURE_PLAYLIST,
        reader_name="local_json",
        platform="apple_music",
        playlist_id="sample",
        playlist_name="示例歌单",
    )


class WebWorkflowSourceTests(unittest.TestCase):
    def test_apple_public_link_does_not_need_a_manual_track_count(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory)

            def export(_url: str, output_path: Path, _expected_count: int | None = None) -> dict[str, object]:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    "Track name,Artist name,Apple - id\nOne,Artist One,1\nTwo,Artist Two,2\n",
                    encoding="utf-8",
                )
                return {"tracks": 2, "completeness_status": "unconfirmed"}

            args = SimpleNamespace(
                source_kind="apple_music",
                source_url="https://music.apple.com/us/playlist/public/pl.fixture",
                expected_count=None,
                playlist_id=None,
                playlist_name=None,
            )
            with mock.patch("web_workflow.export_apple_playlist_file", side_effect=export):
                snapshot, report = _build_source(args, runtime_dir)

            self.assertEqual(snapshot["track_count"], 2)
            self.assertEqual(snapshot["declared_track_count"], 2)
            self.assertEqual(snapshot["reader"]["completeness_status"], "unconfirmed")
            self.assertEqual(report["export"]["completeness_status"], "unconfirmed")

    def test_netease_short_link_resolves_to_canonical_id_without_tracking_query(self) -> None:
        response = mock.Mock()
        response.geturl.return_value = (
            "https://music.163.com/playlist?app_version=9.5.85"
            "&id=7786449876&userid=8157946002"
        )
        opener = mock.MagicMock()
        opener.__enter__.return_value = response
        opener.__exit__.return_value = None
        with mock.patch("web_workflow.urlopen", return_value=opener):
            self.assertEqual(
                _resolve_netease_source("https://163cn.tv/bf2PivdY"),
                ("7786449876", "https://music.163.com/playlist?id=7786449876"),
            )

    def test_netease_short_host_is_allowed_only_for_netease_source(self) -> None:
        self.assertEqual(
            _validate_url("netease_public", "https://163cn.tv/bf2PivdY"),
            "https://163cn.tv/bf2PivdY",
        )
        with self.assertRaises(ContractError):
            _validate_url("apple_music", "https://163cn.tv/bf2PivdY")


class WebWorkflowTrackLimitTests(unittest.TestCase):
    """歌单读完后按网页选择截断：契约仍然成立，来源总数仍可追溯。"""

    def test_track_limit_keeps_step1_contract_and_records_source_total(self) -> None:
        snapshot = _apply_track_limit(fixture_snapshot(), 2)

        self.assertEqual(snapshot["track_count"], 2)
        self.assertEqual(snapshot["declared_track_count"], 2)
        self.assertEqual(len(snapshot["tracks"]), 2)
        self.assertEqual([track["position"] for track in snapshot["tracks"]], [1, 2])
        self.assertEqual(snapshot["reader"]["source_track_count"], FIXTURE_TRACK_COUNT)
        self.assertEqual(snapshot["reader"]["requested_track_limit"], 2)
        self.assertTrue(snapshot["snapshot_id"].endswith("-limit2"))
        validate_playlist_snapshot(snapshot, require_complete=True)

    def test_track_limit_keeps_first_tracks_in_playlist_order(self) -> None:
        full = fixture_snapshot()
        expected = [track["title"] for track in full["tracks"]][:2]

        trimmed = _apply_track_limit(fixture_snapshot(), 2)

        self.assertEqual([track["title"] for track in trimmed["tracks"]], expected)

    def test_track_limit_at_or_above_total_leaves_playlist_intact(self) -> None:
        snapshot = _apply_track_limit(fixture_snapshot(), FIXTURE_TRACK_COUNT)

        self.assertEqual(snapshot["track_count"], FIXTURE_TRACK_COUNT)
        self.assertEqual(len(snapshot["tracks"]), FIXTURE_TRACK_COUNT)
        self.assertIsNone(snapshot["reader"]["requested_track_limit"])
        self.assertEqual(snapshot["reader"]["source_track_count"], FIXTURE_TRACK_COUNT)
        validate_playlist_snapshot(snapshot, require_complete=True)

    def test_track_limit_rejects_values_below_one(self) -> None:
        for invalid in (0, -1, True, "2", None):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                _apply_track_limit(fixture_snapshot(), invalid)

    def test_read_limit_request_enforces_bounds(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME

            path.write_text(json.dumps({"limit": 2}), encoding="utf-8")
            self.assertEqual(_read_limit_request(path, FIXTURE_TRACK_COUNT), 2)

            for invalid in (0, -3, FIXTURE_TRACK_COUNT + 1, "2", 2.5, True, None):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps({"limit": invalid}), encoding="utf-8")
                    with self.assertRaises(ContractError):
                        _read_limit_request(path, FIXTURE_TRACK_COUNT)

            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ContractError):
                _read_limit_request(path, FIXTURE_TRACK_COUNT)

    def test_await_track_limit_returns_submitted_value(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME
            path.write_text(json.dumps({"limit": 2}), encoding="utf-8")

            with mock.patch("web_workflow.emit"):
                self.assertEqual(_await_track_limit(path, FIXTURE_TRACK_COUNT, 5), 2)

    def test_await_track_limit_drops_invalid_request_and_keeps_waiting(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME
            # 超上限的请求应被丢弃，而不是直接让任务失败；随后合法请求生效。
            path.write_text(json.dumps({"limit": FIXTURE_TRACK_COUNT + 5}), encoding="utf-8")

            def submit_valid() -> None:
                time.sleep(0.2)
                path.write_text(json.dumps({"limit": 1}), encoding="utf-8")

            writer = threading.Thread(target=submit_valid)
            writer.start()
            try:
                with mock.patch("web_workflow.emit"):
                    self.assertEqual(_await_track_limit(path, FIXTURE_TRACK_COUNT, 10), 1)
            finally:
                writer.join()

    def test_await_track_limit_times_out(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME

            with self.assertRaises(ContractError):
                _await_track_limit(path, FIXTURE_TRACK_COUNT, 0)

    def test_run_web_workflow_waits_for_limit_then_trims_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_dir = Path(directory) / "job"
            current_data = Path(directory) / "current.json"
            args = SimpleNamespace(
                runtime_dir=str(runtime_dir),
                current_data=str(current_data),
                source_kind="local_json",
                source_url=None,
                input=str(FIXTURE_PLAYLIST),
                playlist_id="sample",
                playlist_name="示例歌单",
                platform="apple_music",
                expected_count=None,
                analysis_command=f"{sys.executable} tests/fixtures/fake_analysis_agent.py",
                recommendation_command=f"{sys.executable} tests/fixtures/fake_agent.py",
                analysis_parallelism=1,
                recommendation_parallelism=3,
                analysis_batch_size=2,
                analysis_context_budget=None,
                analysis_timeout=120,
                context_budget=None,
                recommendation_timeout=120,
                max_research_rounds=2,
                candidate_target=None,
                max_candidates=80,
                await_track_limit=True,
                await_limit_timeout=60,
            )

            def submit_limit_when_ready() -> None:
                request_path = runtime_dir / LIMIT_REQUEST_FILENAME
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if (runtime_dir / "snapshot.json").is_file():
                        request_path.write_text(json.dumps({"limit": 2}), encoding="utf-8")
                        return
                    time.sleep(0.05)

            writer = threading.Thread(target=submit_limit_when_ready)
            writer.start()
            try:
                with mock.patch("web_workflow.emit"):
                    exit_code = run_web_workflow(args)
            finally:
                writer.join()

            self.assertEqual(exit_code, 0)
            snapshot = json.loads((runtime_dir / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["track_count"], 2)
            self.assertEqual(snapshot["declared_track_count"], 2)
            self.assertEqual(len(snapshot["tracks"]), 2)
            self.assertEqual(snapshot["reader"]["source_track_count"], FIXTURE_TRACK_COUNT)
            self.assertEqual(snapshot["reader"]["requested_track_limit"], 2)
            report = json.loads((runtime_dir / "web_job_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["requested_track_limit"], 2)
            self.assertEqual(report["source_track_count"], FIXTURE_TRACK_COUNT)


if __name__ == "__main__":
    unittest.main()
