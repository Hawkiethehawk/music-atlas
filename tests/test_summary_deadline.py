"""A first-run summary may take longer without consuming the publication reserve."""

import unittest

from contracts import ContractError
from web_workflow import (
    AGENT_TIME_BUDGET_SECONDS,
    SUMMARY_AGENT_TIME_BUDGET_SECONDS,
    SMALL_ANALYSIS_TIME_BUDGET_SECONDS,
    analysis_style_timeout,
    analysis_summary_timeout,
    build_parser,
)


class SummaryDeadlineTests(unittest.TestCase):
    def test_small_first_run_style_analysis_reserves_downstream(self):
        self.assertEqual(SMALL_ANALYSIS_TIME_BUDGET_SECONDS, 70)
        self.assertEqual(analysis_style_timeout(7200, 110), 70)
        self.assertEqual(analysis_style_timeout(78, 83.9), 53)
        self.assertEqual(analysis_style_timeout(20, 110), 20)
        with self.assertRaisesRegex(ContractError, "预留 30 秒"):
            analysis_style_timeout(7200, 30.9)

    def test_large_summary_gets_seventy_eight_seconds_when_crawl_leaves_room(self):
        self.assertEqual(analysis_summary_timeout(1917, 7200, 110), 78)
        self.assertEqual(SUMMARY_AGENT_TIME_BUDGET_SECONDS, 78)
        self.assertEqual(AGENT_TIME_BUDGET_SECONDS, 45)

    def test_crawl_and_other_elapsed_time_reduce_summary_allowance(self):
        self.assertEqual(analysis_summary_timeout(1917, 7200, 83.9), 53)
        self.assertEqual(analysis_summary_timeout(210, 7200, 31.9), 1)
        with self.assertRaisesRegex(ContractError, "预留 30 秒"):
            analysis_summary_timeout(1917, 7200, 30.9)

    def test_configured_lower_timeout_is_honored(self):
        self.assertEqual(analysis_summary_timeout(1917, 40, 110), 40)
        self.assertEqual(analysis_summary_timeout(1917, 1000, 110), 78)

    def test_cli_default_does_not_restrict_summary_to_non_summary_cap(self):
        args = build_parser().parse_args(["--runtime-dir", "/tmp/x", "--source-kind", "netease_public"])
        self.assertEqual(args.analysis_timeout, 78)


if __name__ == "__main__":
    unittest.main()
