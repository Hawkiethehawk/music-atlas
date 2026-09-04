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
    _parse_netease_detail,
    _playlist_id_from_arg,
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


if __name__ == "__main__":
    unittest.main()
