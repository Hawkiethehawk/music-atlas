from __future__ import annotations

# 测试夹具曲目是合成数据，平台上不存在；关闭平台元数据核验，
# 核验逻辑本身由 tests/test_metadata_verify.py 与专门用例覆盖。
import os as _atlas_os
_atlas_os.environ.setdefault("ATLAS_METADATA_VERIFY", "off")

import json
from datetime import datetime, timedelta, timezone
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from contracts import ContractError, track_key, validate_playlist_snapshot
from source_adapters import build_snapshot
from web_workflow import (
    LIMIT_REQUEST_FILENAME,
    _apply_track_limit,
    _await_track_limit,
    _build_source,
    _configured_candidate_limits,
    _read_limit_request,
    _read_limit_request_details,
    _percentile_track_limit,
    _exclude_recent_recommendations,
    _recent_recommendation_history,
    _candidate_discovery_limits,
    _curate_review_groups,
    _discover_unique_candidates,
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
    def test_candidate_limits_use_split_initial_and_hard_fields(self) -> None:
        args = SimpleNamespace(initial_candidate_limit=60, hard_candidate_limit=200)
        self.assertEqual(_configured_candidate_limits(args, required=30), (60, 200))

        args = SimpleNamespace(initial_candidate_limit=240, hard_candidate_limit=120)
        with self.assertRaises(ContractError):
            _configured_candidate_limits(args, required=30)

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
            with mock.patch("web_workflow.export_apple_playlist_file", side_effect=export), mock.patch("web_workflow._apple_playlist_name", return_value="Public playlist"):
                snapshot, report = _build_source(args, runtime_dir)

            self.assertEqual(snapshot["playlist_name"], "Public playlist")
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
        with mock.patch("playlist_source.urlopen", return_value=opener):
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


    def test_percentile_limits_round_up(self) -> None:
        self.assertEqual([_percentile_track_limit(123, value) for value in (0.25, 0.5, 1.0)], [31, 62, 123])
        self.assertEqual([_percentile_track_limit(3, value) for value in (0.25, 0.5, 1.0)], [1, 2, 3])
        self.assertEqual([_percentile_track_limit(1, value) for value in (0.25, 0.5, 1.0)], [1, 1, 1])

    def test_percentile_request_must_match_server_calculation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME
            path.write_text(json.dumps({"limit": 31, "percentile": 0.25}), encoding="utf-8")
            self.assertEqual(_read_limit_request_details(path, 123), {"limit": 31, "percentile": 0.25})
            for payload in ({"limit": 30, "percentile": 0.25}, {"limit": 62, "percentile": 0.3}, {"limit": 31}):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ContractError):
                    _read_limit_request_details(path, 123)

    def test_recent_recommendations_are_excluded_by_id_or_track_key(self) -> None:
        candidates = [
            {"canonical_track_id": "id-1", "title": "One", "artist": "Artist A"},
            {"canonical_track_id": "id-2", "title": "Two", "artist": "Artist B"},
            {"canonical_track_id": "id-3", "title": "Three", "artist": "Artist C"},
        ]
        history = {"canonical_track_ids": {"id-1"}, "track_keys": {track_key("Two", "Artist B")}}
        self.assertEqual(
            [item["canonical_track_id"] for item in _exclude_recent_recommendations(candidates, history)],
            ["id-3"],
        )

    def test_recommendation_history_keeps_only_the_last_seven_days(self) -> None:
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps({
                "entries": [
                    {
                        "generated_at": (now - timedelta(days=6, hours=23)).isoformat(),
                        "canonical_track_ids": ["recent-id"],
                        "track_keys": [track_key("Recent", "Artist")],
                    },
                    {
                        "generated_at": (now - timedelta(days=7, seconds=1)).isoformat(),
                        "canonical_track_ids": ["expired-id"],
                        "track_keys": [track_key("Expired", "Artist")],
                    },
                ]
            }), encoding="utf-8")

            history = _recent_recommendation_history(path, now=now)

        self.assertEqual(len(history["entries"]), 1)
        self.assertEqual(history["canonical_track_ids"], {"recent-id"})
        self.assertEqual(history["track_keys"], {track_key("Recent", "Artist")})

    def test_merges_platform_artist_continuation_candidates(self) -> None:
        history = {"entries": [], "canonical_track_ids": set(), "track_keys": set()}
        platform_rows = [
            {"canonical_track_id": f"platform:netease:{index}", "title": f"Continuation {index}",
             "artist": f"Anchor {index}", "candidate_type": "artist_continuation"}
            for index in range(3)
        ]
        similar_rows = [
            {"canonical_track_id": "platform:itunes:1", "title": "Similar One", "artist": "Other", "candidate_type": "style_neighbor"},
            {"canonical_track_id": "platform:netease:0", "title": "Continuation 0", "artist": "Anchor 0", "candidate_type": "artist_continuation"},
        ]
        with mock.patch("lastfm_pipeline.LastFM"), \
             mock.patch("platform_discovery.discover_platform_candidates", return_value=(platform_rows, {})), \
             mock.patch("lastfm_pipeline.discover", return_value=(similar_rows, {"provider": "fixture"})):
            candidates, report = _discover_unique_candidates({}, history, 6, 4, None)

        self.assertEqual(len(candidates), 4, "平台候选应与相似艺人候选合并去重")
        self.assertEqual(report["platform_candidate_count"], 3)
        self.assertEqual(report["artist_continuation_count"], 3, "艺人延伸候选应计入报告")
        self.assertTrue(any(item.get("candidate_type") == "artist_continuation" for item in candidates))

    def test_candidate_discovery_limits_include_verification_headroom(self) -> None:
        empty = {"canonical_track_ids": set(), "track_keys": set()}
        crowded = {"canonical_track_ids": {f"id-{i}" for i in range(120)}, "track_keys": set()}
        saturated = {"canonical_track_ids": {f"id-{i}" for i in range(180)}, "track_keys": set()}

        self.assertEqual(_candidate_discovery_limits(60, 30, empty), [60, 200])
        self.assertEqual(_candidate_discovery_limits(60, 30, crowded), [60, 200])
        self.assertEqual(_candidate_discovery_limits(60, 30, saturated), [60, 200, 330])
        self.assertEqual(
            _candidate_discovery_limits(60, 30, saturated, hard_limit=200),
            [60, 200],
        )

    def test_candidate_discovery_retries_at_hard_cap_after_weekly_exclusion(self) -> None:
        history = {
            "canonical_track_ids": {"id-0", "id-1", "id-2"},
            "track_keys": set(),
        }
        make_candidates = lambda count: [
            {"canonical_track_id": f"id-{index}", "title": f"Track {index}", "artist": f"Artist {index}"}
            for index in range(count)
        ]
        expanded = []
        with mock.patch("lastfm_pipeline.LastFM"), mock.patch(
            "platform_discovery.discover_platform_candidates", return_value=([], {}),
        ), mock.patch(
            "lastfm_pipeline.discover",
            side_effect=[(make_candidates(7), {"provider": "fixture"}),
                         (make_candidates(9), {"provider": "fixture"})],
        ) as discover:
            candidates, report = _discover_unique_candidates(
                {}, history, 6, 5, lambda previous, current, eligible: expanded.append((previous, current, eligible))
            )

        self.assertEqual([call.kwargs["max_candidates"] for call in discover.call_args_list], [6, 200])
        self.assertEqual(expanded, [(6, 200, 4)])
        self.assertEqual(len(candidates), 6)
        self.assertTrue(report["pool_expanded"])
        self.assertEqual(report["eligible_candidate_count"], 6)
        self.assertEqual(len(report["discovery_attempts"]), 2)

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

    def test_read_limit_request_requires_supported_percentile(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME

            path.write_text(json.dumps({"limit": 2, "percentile": 0.5}), encoding="utf-8")
            self.assertEqual(_read_limit_request(path, FIXTURE_TRACK_COUNT), 2)

            invalid_payloads = [
                {"limit": 2},
                {"limit": 1, "percentile": 0.5},
                {"limit": 2, "percentile": 0.3},
                {"limit": 0, "percentile": 0.25},
                {"limit": FIXTURE_TRACK_COUNT + 1, "percentile": 1},
                {"limit": "2", "percentile": 0.5},
            ]
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ContractError):
                        _read_limit_request(path, FIXTURE_TRACK_COUNT)

            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(ContractError):
                _read_limit_request(path, FIXTURE_TRACK_COUNT)

    def test_await_track_limit_returns_submitted_value(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME
            path.write_text(json.dumps({"limit": 2, "percentile": 0.5}), encoding="utf-8")

            with mock.patch("web_workflow.emit"):
                self.assertEqual(_await_track_limit(path, FIXTURE_TRACK_COUNT, 5), 2)

    def test_await_track_limit_drops_invalid_request_and_keeps_waiting(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / LIMIT_REQUEST_FILENAME
            # 非分位请求应被丢弃，而不是直接让任务失败；随后合法分位请求生效。
            path.write_text(json.dumps({"limit": 1}), encoding="utf-8")

            def submit_valid() -> None:
                time.sleep(0.2)
                path.write_text(json.dumps({"limit": 1, "percentile": 0.25}), encoding="utf-8")

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
            policy_file = Path(directory) / "policy.json"
            editorial_file = Path(directory) / "editorial.json"
            # 本用例只验收“完整读取后再限额”的边界；合成歌单没有真实目录
            # 的多艺人/多专辑分布，因此只放宽可调多样性上限，不改固定推荐数量。
            policy_file.write_text(json.dumps({
                "candidate_pool_min": 1,
                "max_per_artist": 10,
                "max_per_project": 10,
                "min_projects": 1,
            }), encoding="utf-8")
            editorial_file.write_text(json.dumps({"title": "测试网页", "lede": "测试导语"}), encoding="utf-8")
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
                policy_file=str(policy_file),
                editorial=str(editorial_file),
                await_track_limit=True,
                await_limit_timeout=60,
            )

            def submit_limit_when_ready() -> None:
                request_path = runtime_dir / LIMIT_REQUEST_FILENAME
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if (runtime_dir / "snapshot.json").is_file():
                        request_path.write_text(json.dumps({"limit": 2, "percentile": 0.5}), encoding="utf-8")
                        return
                    time.sleep(0.05)

            writer = threading.Thread(target=submit_limit_when_ready)
            writer.start()
            try:
                def candidates(packet, *_args, **_kwargs):
                    full_keys = {track["track_key"] for track in fixture_snapshot()["tracks"]}
                    self.assertEqual(packet["playlist_exclusion"]["source_track_count"], FIXTURE_TRACK_COUNT)
                    self.assertEqual(set(packet["playlist_exclusion"]["track_keys"]), full_keys)
                    seed=packet['primary_distribution'][0]
                    url='https://www.last.fm/music/'+__import__('urllib.parse',fromlist=['quote']).quote(seed['artist'],safe='')+'/+similar'
                    rows=[]
                    for number in range(1, 31):
                        title=f'Verified fixture {number}'
                        artist=f'New fixture artist {number}'
                        platform_id=f'fixture-{number}'
                        fact={'title':title,'artist':artist,'album':f'Fixture album {number}','platform_track_id':platform_id,'source':'itunes','url':f'https://music.apple.com/us/song/fixture/{number}'}
                        rows.append({'canonical_track_id':f'platform:itunes:{platform_id}','title':title,'artist':artist,'project':fact['album'],'platform_track_id':platform_id,'metadata_verified':fact,'analysis_refs':[seed['entity_ref']],'provider_similarity':{'seed':seed['artist'],'artist':artist,'url':url},'candidate_type':'style_neighbor','sources':[url,fact['url']],'platform_links':{'itunes':fact['url']},'evidence_items':[]})
                    return rows,{}
                empty_relations = {"schema_version":"2.0","catalog_type":"live_public_relations",
                                   "generated_at":"2026-09-17T00:00:00Z","seed_artists":[],
                                   "artists":{},"unresolved_artists":[],"requests":[]}
                with mock.patch("web_workflow.emit"), mock.patch("agent_lastfm.analyze"), mock.patch("agent_lastfm.curate", side_effect=lambda packet, candidates, *args: candidates), mock.patch("lastfm_pipeline.LastFM"), mock.patch("lastfm_pipeline.validate_knowledge"), mock.patch("lastfm_pipeline.collect_tags",return_value={'records':[]}), mock.patch("lastfm_pipeline.discover",side_effect=candidates), mock.patch("relationship_sources.collect_relationships", return_value=empty_relations), mock.patch("platform_discovery.discover_platform_candidates", return_value=([], {})):
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
            self.assertEqual(snapshot["reader"]["requested_track_percentile"], 0.5)
            report = json.loads((runtime_dir / "web_job_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["requested_track_limit"], 2)
            self.assertEqual(report["requested_track_percentile"], 0.5)
            self.assertEqual(report["source_track_count"], FIXTURE_TRACK_COUNT)
            payload = json.loads((runtime_dir / "web_payload.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["issue"]["title"], "测试网页")
            self.assertEqual(payload["issue"]["lede"], "测试导语")
            self.assertEqual(payload["atlas_group_count"], 3)
            self.assertEqual(len(payload["atlas_groups"]), 3)
            group_ids = [{item["id"] for item in group["recommendations"]} for group in payload["atlas_groups"]]
            self.assertTrue(all(len(ids) == 10 for ids in group_ids))
            self.assertEqual(len(set().union(*group_ids)), 30)
            self.assertEqual(report["recommendation_groups"]["total_unique_recommendation_count"], 30)


class WebWorkflowReviewTimingTests(unittest.TestCase):
    def test_agent_latency_is_not_counted_as_local_review_time(self) -> None:
        candidate = {"canonical_track_id": "candidate-1", "title": "Track", "artist": "Artist"}
        groups = [{"recommendations": [candidate]}]
        candidate_review = {"status": "accepted", "entries": [], "elapsed_ms": 1.25}
        groups_review = {"status": "accepted", "elapsed_ms": 2.5}

        def curate(_packet, rows, *_args):
            # The Agent may take much longer than the local 10-second review budget.
            return rows

        with mock.patch("web_workflow.review_candidates", return_value=candidate_review), \
             mock.patch("web_workflow.review_groups", return_value=groups_review), \
             mock.patch("web_workflow._build_atlas_groups", return_value=groups):
            rows, built_groups, report = _curate_review_groups(
                {}, [candidate], required=1, curate_fn=curate, curate_args=(),
                hydrate_fn=lambda _rows: None, regeneration_event=lambda *_args: None,
                stage="recommendation",
            )

        self.assertEqual(rows, [candidate])
        self.assertEqual(built_groups, groups)
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["elapsed_ms"], 3.75)
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["agent_calls"], 0)


class ApplePlaylistNameTests(unittest.TestCase):
    def test_public_title_and_entities(self):
        from web_workflow import _apple_playlist_name
        response=mock.MagicMock()
        response.__enter__.return_value.read.return_value=b'<meta property="og:title" content="Rock &amp; Roll - Apple Music">'
        with mock.patch('web_workflow.urlopen',return_value=response):
            self.assertEqual(_apple_playlist_name('https://music.apple.com/us/playlist/x/pl.x'),'Rock & Roll')
    def test_unavailable_name_does_not_block_import(self):
        from web_workflow import _apple_playlist_name
        with mock.patch('web_workflow.urlopen',side_effect=OSError('offline')):
            self.assertEqual(_apple_playlist_name('https://music.apple.com/us/playlist/x/pl.x'),'')
    def test_generic_web_player_title_is_ignored(self):
        from web_workflow import _apple_playlist_name
        response=mock.MagicMock()
        response.__enter__.return_value.read.return_value='<meta property="og:title" content="Apple\u00a0Music 网页播放器">'.encode()
        with mock.patch('web_workflow.urlopen',return_value=response):
            self.assertEqual(_apple_playlist_name('https://music.apple.com/us/playlist/x/pl.x'),'')

    def test_exported_csv_playlist_name_is_preferred(self):
        from web_workflow import _apple_export_playlist_name
        with TemporaryDirectory() as td:
            path=Path(td)/'playlist.csv'
            path.write_text('Track name,Artist name,Playlist name\nSong,Artist,Favorite Songs\n',encoding='utf-8')
            self.assertEqual(_apple_export_playlist_name(path),'Favorite Songs')

if __name__ == "__main__":
    unittest.main()
