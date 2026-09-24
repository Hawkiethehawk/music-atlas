from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts import ContractError
from source_adapters import (
    CsvPlaylistReader,
    NeteasePublicPlaylistReader,
    QQPublicPlaylistReader,
    _crawler_url,
    _normalize_csv_key,
    _parse_netease_detail,
    _parse_netease_song_details,
    _parse_qq_diss_page,
    _playlist_id_from_arg,
    _playlist_id_from_qq_arg,
    _request_headers,
)


SAMPLE_DETAIL_PAYLOAD = {
    "code": 200,
    "result": {
        "name": "示例歌单",
        "trackIds": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
        "tracks": [
            {
                "id": "101",
                "name": "Song A",
                "artists": [{"name": "Artist A"}, {"name": "Artist B"}],
                "album": {"name": "Album A"},
            },
            {
                "id": "102",
                "name": "Song B",
                "artists": [{"name": "Artist C"}],
                "album": {"name": "Album B"},
            },
            {"id": "103", "name": "", "artists": [], "album": {}},
            {
                "id": "104",
                "name": "Song D",
                "artists": [{"name": "Artist D"}],
                "album": {"name": "Album D"},
            },
        ],
    },
}

SAMPLE_RESPONSE_BYTES = json.dumps(SAMPLE_DETAIL_PAYLOAD, ensure_ascii=False).encode("utf-8")

V6_DETAIL_PAYLOAD = {
    "code": 200,
    "playlist": {
        "name": "v6 歌单",
        "trackIds": [{"id": 102}, {"id": 101}, {"id": 103}],
        "tracks": [
            {"id": 102, "name": "Song B", "ar": [{"name": "Artist B"}], "al": {"name": "Album B"}},
        ],
    },
}

V3_SONG_DETAIL_PAYLOAD = {
    "code": 200,
    "songs": [
        {"id": 101, "name": "Song A", "ar": [{"name": "Artist A"}], "al": {"name": "Album A"}},
        {"id": 102, "name": "Song B", "ar": [{"name": "Artist B"}], "al": {"name": "Album B"}},
        {"id": 103, "name": "Song C", "ar": [{"name": "Artist C"}], "al": {"name": "Album C"}},
    ],
}


def _crawler_url_from_settings(value: str) -> str:
    """用指定值模拟项目配置，避免读到真实 config/web.json。"""

    with mock.patch("source_adapters.crawler_settings", return_value={"netease_detail_url": value}):
        return _crawler_url(
            "netease_detail_url", "https://music.163.com/api/v6/playlist/detail", ("music.163.com",)
        )


class CrawlerUrlTests(unittest.TestCase):
    """网页保存的爬虫地址必须被读取器接受（包括去掉末尾斜杠的 Referer）。"""

    def test_referer_without_trailing_slash_is_accepted(self) -> None:
        with mock.patch("source_adapters.crawler_settings", return_value={
            "netease_referer": "https://music.163.com",
            "qq_referer": "https://y.qq.com",
        }):
            netease = _request_headers("netease")
            qq = _request_headers("qq")

        self.assertEqual(netease["Referer"], "https://music.163.com")
        self.assertEqual(qq["Referer"], "https://y.qq.com")

    def test_absolute_and_relative_paths_are_both_accepted(self) -> None:
        for value, expected in (
            ("https://music.163.com/", "https://music.163.com"),
            ("https://music.163.com", "https://music.163.com"),
            ("https://music.163.com/api/v6/playlist/detail", "https://music.163.com/api/v6/playlist/detail"),
        ):
            with self.subTest(value=value):
                self.assertEqual(_crawler_url_from_settings(value), expected)

    def test_other_hosts_and_schemes_are_rejected(self) -> None:
        for value in (
            "https://evil.example.com",
            "http://music.163.com",
            "https://music.163.com.evil.example.com",
            "https://u.y.qq.com/api",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    _crawler_url_from_settings(value)


class PlaylistIdParsingTests(unittest.TestCase):
    def test_plain_numeric_id(self) -> None:
        self.assertEqual(_playlist_id_from_arg("3778678"), "3778678")

    def test_share_url_query_form(self) -> None:
        self.assertEqual(
            _playlist_id_from_arg("https://music.163.com/playlist?id=3778678"),
            "3778678",
        )

    def test_share_url_hash_form(self) -> None:
        self.assertEqual(
            _playlist_id_from_arg("https://music.163.com#/playlist/3778678/"),
            "3778678",
        )

    def test_unparsable_argument_raises(self) -> None:
        with self.assertRaises(ContractError):
            _playlist_id_from_arg("not-a-playlist")


class CsvHeaderNormalizationTests(unittest.TestCase):
    def test_tunemymusic_headers_map_to_candidates(self) -> None:
        self.assertEqual(_normalize_csv_key("Track name"), "track_name")
        self.assertEqual(_normalize_csv_key("Artist name"), "artist_name")
        self.assertEqual(_normalize_csv_key("Apple - id"), "apple_id")
        self.assertEqual(_normalize_csv_key("Album"), "album")

    def test_tunemymusic_export_parses_with_stable_ids(self) -> None:
        # TuneMyMusic 真实导出表头：Track name, Artist name, Album,
        # Playlist name, Type, ISRC, Apple - id
        import csv as csv_module
        import tempfile

        sample = (
            "Track name,Artist name,Album,Playlist name,Type,ISRC,Apple - id\n"
            "Song One,Artist One,Album One,PL,Playlist,AA111,1111111111\n"
            "Song Two,Artist Two,Album Two,PL,Playlist,AA222,2222222222\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            path.write_text(sample, encoding="utf-8-sig")
            snapshot = CsvPlaylistReader().read(
                path,
                platform="apple_music",
                playlist_id="pl.u-test",
                playlist_name="PL",
            )
        self.assertEqual(snapshot["reader_status"], "complete")
        self.assertEqual(snapshot["declared_track_count"], 2)
        self.assertEqual(snapshot["track_count"], 2)
        first = snapshot["tracks"][0]
        self.assertEqual(first["title"], "Song One")
        self.assertEqual(first["artist"], "Artist One")
        self.assertEqual(first["platform_track_id"], "1111111111")


class NeteaseDetailParsingTests(unittest.TestCase):
    def test_tracks_are_normalized_and_bad_rows_skipped(self) -> None:
        tracks, declared, playlist_name = _parse_netease_detail(SAMPLE_DETAIL_PAYLOAD)
        self.assertEqual(playlist_name, "示例歌单")
        self.assertEqual(declared, 4)
        self.assertEqual(len(tracks), 3)  # 一行缺歌名与艺人，跳过
        first = tracks[0]
        self.assertEqual(first["title"], "Song A")
        self.assertEqual(first["artists"], ["Artist A", "Artist B"])
        self.assertEqual(first["artist"], "Artist A")
        self.assertEqual(first["platform_track_id"], "101")
        self.assertEqual(
            first["links"], {"netease": "https://music.163.com/song?id=101"}
        )
        self.assertEqual(first["track_key"], "song a\u001fartist a")

    def test_non_200_code_raises(self) -> None:
        with self.assertRaises(ContractError):
            _parse_netease_detail({"code": 301})

    def test_missing_result_raises(self) -> None:
        with self.assertRaises(ContractError):
            _parse_netease_detail({"code": 200})

    def test_missing_track_ids_falls_back_to_actual_count(self) -> None:
        payload = {
            "code": 200,
            "result": {
                "name": "OnlyTracks",
                "tracks": SAMPLE_DETAIL_PAYLOAD["result"]["tracks"],
            },
        }
        tracks, declared, _ = _parse_netease_detail(payload)
        self.assertEqual(len(tracks), 3)
        self.assertEqual(declared, 3)

    def test_v6_playlist_and_v3_song_shapes_are_normalized(self) -> None:
        tracks, declared, playlist_name = _parse_netease_detail(V6_DETAIL_PAYLOAD)
        self.assertEqual(playlist_name, "v6 歌单")
        self.assertEqual(declared, 3)
        self.assertEqual(tracks[0]["artist"], "Artist B")
        details = _parse_netease_song_details(V3_SONG_DETAIL_PAYLOAD)
        self.assertEqual([item["platform_track_id"] for item in details], ["101", "102", "103"])


class NeteasePublicReaderTests(unittest.TestCase):
    @staticmethod
    def _multi_batch_payload(count: int) -> bytes:
        return json.dumps({
            "code": 200,
            "playlist": {
                "name": "并行歌单", "trackCount": count,
                "trackIds": [{"id": 100 + index} for index in range(1, count + 1)],
                "tracks": [],
            },
        }).encode("utf-8")

    @staticmethod
    def _song_detail(track_id: int, *, missing: bool = False) -> bytes:
        songs = [] if missing else [{
            "id": track_id, "name": f"Song {track_id}",
            "ar": [{"name": "Artist"}], "al": {"name": "Album"},
        }]
        return json.dumps({"code": 200, "songs": songs}).encode("utf-8")

    def test_parallel_batches_preserve_order_hash_and_bound(self) -> None:
        source = self._multi_batch_payload(4)
        responses = {str(track_id): self._song_detail(track_id) for track_id in range(101, 105)}
        barrier = threading.Barrier(3)
        guard = threading.Lock()
        active = 0
        maximum_active = 0
        finishes: list[str] = []

        def fetch(ids: list[str]) -> bytes:
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            if ids[0] in ("101", "102", "103"):
                barrier.wait(timeout=2)
                time.sleep((104 - int(ids[0])) * 0.015)
            with guard:
                active -= 1
                finishes.append(ids[0])
            return responses[ids[0]]

        with mock.patch("source_adapters.crawler_settings", return_value={"netease_song_detail_batch_size": 1}), \
             mock.patch("source_adapters._fetch_netease_playlist_detail", return_value=source), \
             mock.patch("source_adapters._fetch_netease_song_details", side_effect=fetch):
            snapshot = NeteasePublicPlaylistReader().read(
                None, platform="netease", playlist_id="7786449876", playlist_name="忽略"
            )
        self.assertEqual([row["platform_track_id"] for row in snapshot["tracks"]], ["101", "102", "103", "104"])
        self.assertEqual([row["position"] for row in snapshot["tracks"]], [1, 2, 3, 4])
        self.assertEqual(snapshot["reader"]["song_detail_request_count"], 4)
        self.assertEqual(maximum_active, 3)
        self.assertNotEqual(finishes[:3], ["101", "102", "103"])
        digest = hashlib.sha256()
        for raw in [source, *[responses[str(track_id)] for track_id in range(101, 105)]]:
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        self.assertEqual(snapshot["input_sha256"], digest.hexdigest())
        self.assertEqual(snapshot["snapshot_id"], f"netease-{digest.hexdigest()[:16]}")

    def test_missing_song_remains_recorded_without_partial_fetch(self) -> None:
        source = self._multi_batch_payload(4)

        def fetch(ids: list[str]) -> bytes:
            track_id = int(ids[0])
            return self._song_detail(track_id, missing=track_id == 103)

        with mock.patch("source_adapters.crawler_settings", return_value={"netease_song_detail_batch_size": 1}), \
             mock.patch("source_adapters._fetch_netease_playlist_detail", return_value=source), \
             mock.patch("source_adapters._fetch_netease_song_details", side_effect=fetch):
            snapshot = NeteasePublicPlaylistReader().read(
                None, platform="netease", playlist_id="7786449876", playlist_name="忽略"
            )
        self.assertEqual([row["platform_track_id"] for row in snapshot["tracks"]], ["101", "102", "104"])
        self.assertEqual(snapshot["reader"]["unavailable_track_ids"], ["103"])
        self.assertEqual(snapshot["reader"]["song_detail_request_count"], 4)

    def test_http_429_stops_parallel_scheduling_and_retries_serially(self) -> None:
        from urllib.error import HTTPError

        source = self._multi_batch_payload(4)
        guard = threading.Lock()
        attempts: dict[str, int] = {}
        barrier = threading.Barrier(3)
        calls: list[str] = []

        def fetch(ids: list[str]) -> bytes:
            track_id = ids[0]
            with guard:
                attempts[track_id] = attempts.get(track_id, 0) + 1
                calls.append(track_id)
                attempt = attempts[track_id]
            if track_id in ("101", "102", "103") and attempt == 1:
                barrier.wait(timeout=2)
            if track_id == "103" and attempt == 1:
                try:
                    raise HTTPError("https://music.163.com", 429, "rate limited", None, None)
                except HTTPError as exc:
                    raise ContractError("详情请求限流") from exc
            if track_id in ("101", "102") and attempt == 1:
                time.sleep(0.03)
            return self._song_detail(int(track_id))

        with mock.patch("source_adapters.crawler_settings", return_value={"netease_song_detail_batch_size": 1}), \
             mock.patch("source_adapters._fetch_netease_playlist_detail", return_value=source), \
             mock.patch("source_adapters._fetch_netease_song_details", side_effect=fetch):
            snapshot = NeteasePublicPlaylistReader().read(
                None, platform="netease", playlist_id="7786449876", playlist_name="忽略"
            )
        self.assertEqual([row["platform_track_id"] for row in snapshot["tracks"]], ["101", "102", "103", "104"])
        self.assertEqual(attempts, {"101": 1, "102": 1, "103": 2, "104": 1})
        self.assertEqual(calls[-2:], ["103", "104"])
        self.assertEqual(snapshot["reader"]["song_detail_request_count"], 4)

    def test_api_429_does_not_become_missing_song(self) -> None:
        with mock.patch("source_adapters.crawler_settings", return_value={"netease_song_detail_batch_size": 1}), \
             mock.patch("source_adapters._fetch_netease_playlist_detail", return_value=self._multi_batch_payload(2)), \
             mock.patch("source_adapters._fetch_netease_song_details", return_value=b'{"code":429}'), \
             mock.patch("source_adapters.time.sleep"):
            with self.assertRaisesRegex(ContractError, "429"):
                NeteasePublicPlaylistReader().read(
                    None, platform="netease", playlist_id="7786449876", playlist_name="忽略"
                )

    def test_read_builds_snapshot_with_declared_mismatch_incomplete(self) -> None:
        reader = NeteasePublicPlaylistReader()
        with mock.patch(
            "source_adapters._fetch_netease_playlist_detail",
            return_value=SAMPLE_RESPONSE_BYTES,
        ):
            snapshot = reader.read(
                None,
                platform="netease",
                playlist_id="3778678",
                playlist_name="忽略的名称",
            )
        self.assertEqual(snapshot["reader_status"], "incomplete")
        self.assertEqual(snapshot["declared_track_count"], 4)
        self.assertEqual(snapshot["track_count"], 3)
        self.assertEqual(snapshot["playlist_id"], "3778678")
        self.assertEqual(snapshot["playlist_name"], "示例歌单")
        self.assertEqual(snapshot["reader"]["type"], "netease_public")
        self.assertEqual(snapshot["reader"]["declared_count_source"], "api_track_ids")
        self.assertEqual(snapshot["tracks"][0]["platform_track_id"], "101")
        self.assertTrue(snapshot["snapshot_id"].startswith("netease-"))
        self.assertTrue(snapshot["input_sha256"])

    def test_read_explicit_declared_count_overrides_api(self) -> None:
        reader = NeteasePublicPlaylistReader()
        with mock.patch(
            "source_adapters._fetch_netease_playlist_detail",
            return_value=SAMPLE_RESPONSE_BYTES,
        ):
            snapshot = reader.read(
                None,
                platform="netease",
                playlist_id="3778678",
                playlist_name="示例",
                declared_count=3,
            )
        self.assertEqual(snapshot["declared_track_count"], 3)
        self.assertEqual(snapshot["reader_status"], "complete")
        self.assertEqual(snapshot["reader"]["declared_count_source"], "argument")

    def test_v6_reader_fetches_missing_tracks_and_restores_playlist_order(self) -> None:
        reader = NeteasePublicPlaylistReader()
        with mock.patch(
            "source_adapters._fetch_netease_playlist_detail",
            return_value=json.dumps(V6_DETAIL_PAYLOAD).encode("utf-8"),
        ), mock.patch(
            "source_adapters._fetch_netease_song_details",
            return_value=json.dumps(V3_SONG_DETAIL_PAYLOAD).encode("utf-8"),
        ) as fetch_details:
            snapshot = reader.read(
                None,
                platform="netease",
                playlist_id="7786449876",
                playlist_name="忽略的名称",
            )
        self.assertEqual(snapshot["reader_status"], "complete")
        self.assertEqual(snapshot["declared_track_count"], 3)
        self.assertEqual(snapshot["track_count"], 3)
        self.assertEqual([item["platform_track_id"] for item in snapshot["tracks"]], ["102", "101", "103"])
        fetch_details.assert_called_once_with(["101", "103"])
        self.assertEqual(snapshot["reader"]["song_detail_request_count"], 1)
        self.assertEqual(snapshot["reader"]["embedded_track_count"], 1)
        self.assertEqual(snapshot["reader"]["requested_song_detail_id_count"], 2)

    def test_large_playlist_reuses_embedded_tracks_but_keeps_full_snapshot_before_half_selection(self) -> None:
        from contracts import validate_playlist_snapshot
        from web_workflow import _apply_track_limit

        raw_songs = [
            {"id": 10_000 + index, "name": f"Song {index}",
             "ar": [{"name": f"Artist {index}"}], "al": {"name": f"Album {index}"}}
            for index in range(1917)
        ]
        playlist_raw = json.dumps({"code": 200, "playlist": {
            "name": "large fixture", "trackIds": [{"id": item["id"]} for item in raw_songs],
            "tracks": raw_songs[:1000],
        }}).encode("utf-8")
        requested: list[list[str]] = []

        def fetch(ids: list[str]) -> bytes:
            requested.append(ids)
            return json.dumps({"code": 200, "songs": [raw_songs[int(track_id) - 10_000]
                                                     for track_id in ids]}).encode("utf-8")

        with mock.patch("source_adapters._fetch_netease_playlist_detail", return_value=playlist_raw), \
             mock.patch("source_adapters._fetch_netease_song_details", side_effect=fetch):
            snapshot = NeteasePublicPlaylistReader().read(
                None, platform="netease", playlist_id="7786449876", playlist_name="ignored")
        validate_playlist_snapshot(snapshot, require_complete=True)
        self.assertEqual(snapshot["declared_track_count"], 1917)
        self.assertEqual(snapshot["track_count"], 1917)
        self.assertEqual([item["platform_track_id"] for item in snapshot["tracks"]],
                         [str(item["id"]) for item in raw_songs])
        self.assertEqual([item["position"] for item in snapshot["tracks"]], list(range(1, 1918)))
        self.assertEqual(snapshot["reader"]["embedded_track_count"], 1000)
        self.assertEqual(snapshot["reader"]["requested_song_detail_id_count"], 917)
        self.assertEqual(snapshot["reader"]["song_detail_request_count"], 5)
        self.assertEqual(sorted(track_id for batch in requested for track_id in batch),
                         sorted(str(item["id"]) for item in raw_songs[1000:]))
        self.assertEqual(snapshot["reader"]["song_detail_batch_attempt_counts"], [1] * 5)
        self.assertEqual(len(snapshot["reader"]["song_detail_batch_elapsed_seconds"]), 5)
        self.assertGreaterEqual(snapshot["reader"]["playlist_detail_elapsed_seconds"], 0)
        full_snapshot_id = snapshot["snapshot_id"]
        full_playlist_tracks = list(snapshot["tracks"])
        limited = _apply_track_limit(snapshot, 959, percentile=0.5)
        validate_playlist_snapshot(limited, require_complete=True)
        self.assertEqual(limited["reader"]["source_track_count"], 1917)
        self.assertEqual(limited["track_count"], 959)
        self.assertEqual(limited["snapshot_id"], f"{full_snapshot_id}-limit959")
        self.assertEqual(len(full_playlist_tracks), 1917)
        self.assertEqual(full_playlist_tracks[-1]["platform_track_id"], "11916")
        self.assertNotIn(full_playlist_tracks[-1]["platform_track_id"],
                         {track["platform_track_id"] for track in limited["tracks"]})

    def test_conflicting_embedded_identity_is_refetched_before_publication(self) -> None:
        playlist = {"code": 200, "playlist": {
            "name": "conflicting fixture", "trackIds": [{"id": 101}, {"id": 102}],
            "tracks": [
                {"id": 101, "name": "Wrong A", "ar": [{"name": "A"}]},
                {"id": 101, "name": "Wrong B", "ar": [{"name": "B"}]},
                {"id": 102, "name": "Safe", "ar": [{"name": "C"}]},
            ],
        }}
        details = {"code": 200, "songs": [
            {"id": 101, "name": "Confirmed", "ar": [{"name": "Actual"}]},
        ]}
        with mock.patch("source_adapters._fetch_netease_playlist_detail",
                        return_value=json.dumps(playlist).encode()), \
             mock.patch("source_adapters._fetch_netease_song_details",
                        return_value=json.dumps(details).encode()) as fetch_details:
            snapshot = NeteasePublicPlaylistReader().read(
                None, platform="netease", playlist_id="7786449876", playlist_name="ignored")
        fetch_details.assert_called_once_with(["101"])
        self.assertEqual(snapshot["reader"]["embedded_track_count"], 1)
        self.assertEqual([(track["title"], track["artist"]) for track in snapshot["tracks"]],
                         [("Confirmed", "Actual"), ("Safe", "C")])


QQ_SAMPLE_PAGE = {
    "code": 0,
    "req_1": {
        "code": 0,
        "data": {
            "dirinfo": {"title": "示例歌单", "id": 7399480361},
            "total_song_num": 3,
            "hasmore": 0,
            "songlist": [
                {
                    "id": 713483521,
                    "mid": "000AfY3z0b31Gh",
                    "name": "Song One",
                    "singer": [{"name": "Singer X", "mid": "0027jfDn2srFZs"}, {"name": "Singer Y", "mid": "abc"}],
                    "album": {"name": "Album One"},
                },
                {
                    "id": 713483522,
                    "mid": "000BbY3z0b31Gh",
                    "name": "Song Two",
                    "singer": [{"name": "Singer Z", "mid": "003"}],
                    "album": "Album Two",
                },
                {"id": 713483523, "mid": "", "name": "", "singer": [], "album": ""},
            ],
        },
    },
}

QQ_SAMPLE_RESPONSE_BYTES = json.dumps(QQ_SAMPLE_PAGE, ensure_ascii=False).encode("utf-8")


class QQPlaylistIdParsingTests(unittest.TestCase):
    def test_plain_numeric_id(self) -> None:
        self.assertEqual(_playlist_id_from_qq_arg("7399480361"), "7399480361")

    def test_path_form_share_url(self) -> None:
        self.assertEqual(
            _playlist_id_from_qq_arg("https://y.qq.com/n/ryqq/playlist/7399480361"),
            "7399480361",
        )

    def test_query_form_share_url(self) -> None:
        self.assertEqual(
            _playlist_id_from_qq_arg("https://i.y.qq.com/n2/m/share/html/taoge.html?id=7399480361"),
            "7399480361",
        )

    def test_unparsable_argument_raises(self) -> None:
        with self.assertRaises(ContractError):
            _playlist_id_from_qq_arg("not-a-playlist")


class QQDissParsingTests(unittest.TestCase):
    def test_songs_are_normalized_and_bad_rows_skipped(self) -> None:
        page = _parse_qq_diss_page(QQ_SAMPLE_PAGE)
        self.assertEqual(page["playlist_name"], "示例歌单")
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["hasmore"], False)  # hasmore=0 明确为假
        self.assertEqual(page["raw_count"], 3)
        tracks = page["tracks"]
        self.assertEqual(len(tracks), 2)  # 一行缺歌名与艺人，跳过
        first = tracks[0]
        self.assertEqual(first["title"], "Song One")
        self.assertEqual(first["artists"], ["Singer X", "Singer Y"])
        self.assertEqual(first["platform_track_id"], "000AfY3z0b31Gh")
        self.assertEqual(
            first["links"],
            {"qq_music": "https://y.qq.com/n/ryqq/songDetail/000AfY3z0b31Gh"},
        )
        # 字符串型 album 字段也能归一化
        self.assertEqual(tracks[1]["album"], "Album Two")

    def test_gateway_rejection_raises(self) -> None:
        with self.assertRaises(ContractError):
            _parse_qq_diss_page({"code": 0, "req_1": {"code": 500003}})

    def test_top_level_error_raises(self) -> None:
        with self.assertRaises(ContractError):
            _parse_qq_diss_page({"code": 2001})


class QQPublicReaderTests(unittest.TestCase):
    def test_read_builds_snapshot_and_renumbers_positions(self) -> None:
        reader = QQPublicPlaylistReader()
        with mock.patch(
            "source_adapters._fetch_qq_playlist_page",
            return_value=QQ_SAMPLE_RESPONSE_BYTES,
        ) as fetch:
            snapshot = reader.read(
                None,
                platform="qq_music",
                playlist_id="7399480361",
                playlist_name="忽略的名称",
            )
        self.assertEqual(snapshot["reader_status"], "incomplete")  # 3 声明 vs 2 实际
        self.assertEqual(snapshot["declared_track_count"], 3)
        self.assertEqual(snapshot["track_count"], 2)
        self.assertEqual(snapshot["playlist_id"], "7399480361")
        self.assertEqual(snapshot["playlist_name"], "示例歌单")
        self.assertEqual(snapshot["reader"]["type"], "qq_public")
        self.assertEqual(snapshot["reader"]["declared_count_source"], "api_total_song_num")
        self.assertEqual([t["position"] for t in snapshot["tracks"]], [1, 2])
        self.assertTrue(snapshot["snapshot_id"].startswith("qq_music-"))
        fetch.assert_called_once()  # hasmore=0/收集满即停，不再拉第二页

    def test_read_explicit_declared_count_overrides_api(self) -> None:
        reader = QQPublicPlaylistReader()
        with mock.patch(
            "source_adapters._fetch_qq_playlist_page",
            return_value=QQ_SAMPLE_RESPONSE_BYTES,
        ):
            snapshot = reader.read(
                None,
                platform="qq_music",
                playlist_id="7399480361",
                playlist_name="示例",
                declared_count=2,
            )
        self.assertEqual(snapshot["declared_track_count"], 2)
        self.assertEqual(snapshot["reader_status"], "complete")
        self.assertEqual(snapshot["reader"]["declared_count_source"], "argument")


if __name__ == "__main__":
    unittest.main()
