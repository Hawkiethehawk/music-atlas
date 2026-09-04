from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts import ContractError
from source_adapters import (
    NeteasePublicPlaylistReader,
    QQPublicPlaylistReader,
    _parse_netease_detail,
    _parse_qq_diss_page,
    _playlist_id_from_arg,
    _playlist_id_from_qq_arg,
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


class NeteasePublicReaderTests(unittest.TestCase):
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
