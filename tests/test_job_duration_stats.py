#!/usr/bin/env python3
"""Tests for job duration stats by job_name prefix."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import history


class JobDurationStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        history.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _insert(self, job_name, start, end, state="COMPLETED"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, user, job_name, state,
                    start_time_ms, end_time_ms, submit_time_ms,
                    req_gpus, req_cpus, req_mem, req_cpuonly,
                    time_limit_ms, priority, array_task_count,
                    allocations_json, pending_reason, last_seen_ms
                ) VALUES (?, 'u', ?, ?, ?, ?, ?, 0, 1, 0, 0, NULL, 0, 1, '[]', '', ?)
                """,
                (f"j-{job_name}-{start}", job_name, state, start, end, start - 1000, end),
            )

    def test_groups_by_prefix_and_computes_stats(self):
        prefix = "abcdefghij0123456789abcdefghij01234"  # exactly 35 chars
        self.assertEqual(len(prefix), 35)
        # 2h and 4h -> mean 3h, std 1h
        self._insert(prefix, 0, 2 * 3600000)
        self._insert(prefix + "-extra", 0, 4 * 3600000)
        stats = history.get_job_duration_stats_by_prefix(self.db_path, prefix_len=35)
        self.assertIn(prefix, stats)
        s = stats[prefix]
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["mean_h"], 3.0, places=5)
        self.assertAlmostEqual(s["std_h"], 1.0, places=5)

    def test_ignores_non_completed_and_invalid_durations(self):
        self._insert("eval", 0, 3600000, state="FAILED")
        self._insert("eval", 1000, 500, state="COMPLETED")  # end < start
        stats = history.get_job_duration_stats_by_prefix(self.db_path)
        self.assertNotIn("eval", stats)

    def test_single_sample_has_zero_std(self):
        self._insert("brand-new-task", 0, 5 * 3600000)
        stats = history.get_job_duration_stats_by_prefix(self.db_path)
        key = "brand-new-task"
        self.assertEqual(stats[key]["n"], 1)
        self.assertAlmostEqual(stats[key]["mean_h"], 5.0, places=5)
        self.assertAlmostEqual(stats[key]["std_h"], 0.0, places=5)


if __name__ == "__main__":
    unittest.main()
