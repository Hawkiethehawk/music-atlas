from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from contracts import ContractError
from web_workflow import _build_source, _resolve_netease_source, _validate_url


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


if __name__ == "__main__":
    unittest.main()
