from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import web_workflow
from contracts import ContractError


class PublishDeadlineTests(unittest.TestCase):
    def test_commit_within_deadline_publishes_all_success_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new.json"
            target = root / "current.json"
            history_path = root / "recommendation-history.json"
            report_path = root / "web_job_report.json"
            source.write_text("new Atlas", encoding="utf-8")

            web_workflow._commit_completed_workflow(
                payload_path=source,
                current_data_path=target,
                history_path=history_path,
                history={"entries": []},
                groups=[],
                playlist_identity={"kind": "fixture"},
                report_path=report_path,
                report={"status": "completed"},
                deadline=web_workflow.time.monotonic() + 30,
            )

            self.assertEqual(target.read_text(encoding="utf-8"), "new Atlas")
            self.assertEqual(len(web_workflow.read_json(history_path)["entries"]), 1)
            self.assertEqual(web_workflow.read_json(report_path)["status"], "completed")
            self.assertEqual(list(root.glob(".*.tmp")), [])
            self.assertEqual(list(root.glob(".*.bak")), [])

    def _run_commit_with_late_operation(self, delayed_operation: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new.json"
            target = root / "current.json"
            history_path = root / "recommendation-history.json"
            report_path = root / "web_job_report.json"
            source.write_text("new Atlas", encoding="utf-8")
            target.write_text("previous Atlas", encoding="utf-8")
            history_path.write_text("previous history", encoding="utf-8")
            report_path.write_text("previous report", encoding="utf-8")

            clock = [100.0]
            real_copy = web_workflow.shutil.copyfile
            real_replace = web_workflow.os.replace

            def delayed_copy(source_path: Path, temporary_path: Path) -> Path:
                result = real_copy(source_path, temporary_path)
                if delayed_operation == "copy":
                    clock[0] = 121.0
                return result

            def delayed_replace(source_path: Path, target_path: Path) -> None:
                real_replace(source_path, target_path)
                if delayed_operation == "replace":
                    clock[0] = 121.0

            with mock.patch("web_workflow.time.monotonic", side_effect=lambda: clock[0]), \
                    mock.patch("web_workflow.shutil.copyfile", side_effect=delayed_copy), \
                    mock.patch("web_workflow.os.replace", side_effect=delayed_replace):
                with self.assertRaisesRegex(ContractError, "最终 Atlas 提交|提交确认"):
                    web_workflow._commit_completed_workflow(
                        payload_path=source,
                        current_data_path=target,
                        history_path=history_path,
                        history={"entries": []},
                        groups=[],
                        playlist_identity={"kind": "fixture"},
                        report_path=report_path,
                        report={"status": "completed"},
                        deadline=120.0,
                    )

            self.assertEqual(target.read_text(encoding="utf-8"), "previous Atlas")
            self.assertEqual(history_path.read_text(encoding="utf-8"), "previous history")
            self.assertEqual(report_path.read_text(encoding="utf-8"), "previous report")
            self.assertEqual(list(root.glob(".*.tmp")), [])
            self.assertEqual(list(root.glob(".*.bak")), [])

    def test_timeout_during_copy_preserves_all_published_state(self) -> None:
        self._run_commit_with_late_operation("copy")

    def test_timeout_during_replace_rolls_back_all_published_state(self) -> None:
        self._run_commit_with_late_operation("replace")


if __name__ == "__main__":
    unittest.main()
