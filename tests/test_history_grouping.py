"""Tests for assignment-aware unit/project grouping in usage buckets."""
import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history

H = 3_600_000
D = 86_400_000


def _gpu_hours_from_buckets(data: dict, key: str) -> float:
    bs_h = data["bucket_size_ms"] / H
    return sum(data["users"].get(key, [])) * bs_h


def _sql_unit_gpu_hours(db: str, unit_id: int, from_ms: int, to_ms: int, now_ms: int) -> float:
    conn = sqlite3.connect(db)
    row = conn.execute(
        """
        WITH latest AS (
            SELECT j.*, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY submit_time_ms DESC) rn
            FROM jobs j WHERE j.start_time_ms IS NOT NULL
        )
        SELECT SUM(l.req_gpus * (
            MIN(COALESCE(l.end_time_ms, ?), ?) - MAX(l.start_time_ms, ?)
        ) / 3600000.0)
        FROM latest l
        JOIN project_assignments pa ON pa.user = l.user
          AND pa.start_time_ms <= l.start_time_ms
          AND (pa.end_time_ms IS NULL OR pa.end_time_ms > l.start_time_ms)
        JOIN projects p ON p.id = pa.project_id AND p.unit_id = ?
        WHERE l.rn = 1 AND l.start_time_ms < ? AND COALESCE(l.end_time_ms, ?) > ?
        """,
        (now_ms, to_ms, from_ms, unit_id, to_ms, now_ms, from_ms),
    ).fetchone()
    conn.close()
    return float(row[0] or 0)


class TestAssignmentSegments(unittest.TestCase):
    def test_mid_bucket_assignment_split(self):
        proj_map = {
            "alice": [
                (500, None, 1, "Proj", 0, 10, "Golf Unit", "#f00"),
            ]
        }
        segs = list(history._iter_assignment_segments("alice", 0, 1000, proj_map, "unit", {10: 1}))
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0][2], "Unassigned")
        self.assertEqual(segs[0][1] - segs[0][0], 500)
        self.assertEqual(segs[1][2], "Golf Unit")
        self.assertEqual(segs[1][1] - segs[1][0], 500)


class TestUsageGroupingIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        history.init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO meta (id, nodes_json, gpus_per_node, cpus_per_node, updated_at_ms) VALUES (1, '[\"gpu01\"]', 8, 224, 0)")
        conn.execute("INSERT INTO research_units (id, title, color) VALUES (8, 'Golf Unit', '#abc')")
        conn.execute("INSERT INTO projects (id, title, unit_id, sort_order) VALUES (16, 'Golf Project', 8, 0)")
        from_ms = 1_000_000_000_000
        to_ms = from_ms + D
        now_ms = to_ms
        self.from_ms = from_ms
        self.to_ms = to_ms
        self.now_ms = now_ms
        # Assignment starts halfway through the only daily bucket
        conn.execute(
            "INSERT INTO project_assignments (project_id, user, start_time_ms, end_time_ms) VALUES (16, 'alice', ?, NULL)",
            (from_ms + D // 2,),
        )
        # 1 GPU for 24h; assignment covers second 12h only → 12 GPU-hours for unit
        conn.execute(
            """INSERT INTO jobs (
                job_id, user, job_name, state, start_time_ms, end_time_ms, submit_time_ms,
                req_gpus, req_cpus, last_seen_ms
            ) VALUES ('job1', 'alice', 't', 'COMPLETED', ?, ?, ?, 1, 1, ?)""",
            (from_ms, to_ms, from_ms, now_ms),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)

    def test_wait_buckets_project_grouping_no_keyerror(self):
        """Regression: wait queue chart must not pass a throwaway CPU dict."""
        data = history.get_wait_buckets(self.db, self.from_ms, self.to_ms, group_by="project")
        self.assertIsInstance(data["users"], dict)
        self.assertIn("stats", data)

    def test_wait_stats_use_start_time_cohort(self):
        """Wait stats include jobs started in-range, regardless of submission date."""
        conn = sqlite3.connect(self.db)
        conn.executemany(
            """INSERT INTO jobs (
                job_id, user, job_name, state, start_time_ms, end_time_ms, submit_time_ms,
                req_gpus, req_cpus, last_seen_ms
            ) VALUES (?, 'alice', 't', 'COMPLETED', ?, ?, ?, 8, 1, ?)""",
            [
                # Submitted before the range, but started in it after a 3-hour wait.
                ('old-submit', self.from_ms + 2 * H, self.from_ms + 3 * H,
                 self.from_ms - H, self.now_ms),
                # Submitted in the range, but started after it; must not affect the range.
                ('late-start', self.to_ms + H, self.to_ms + 2 * H,
                 self.from_ms + H, self.now_ms),
            ],
        )
        conn.commit()
        conn.close()

        data = history.get_wait_buckets(self.db, self.from_ms, self.to_ms)
        large = data["stats_8gpu"]["alice"]
        self.assertEqual(large["job_count"], 1)
        self.assertAlmostEqual(large["avg"], 3.0)

        series = data["wait_time_buckets"]["alice"]
        start_bucket = 2 * H // data["bucket_size_ms"]
        self.assertEqual(series["gte8_count"][start_bucket], 1)
        self.assertAlmostEqual(series["gte8_sum"][start_bucket], 3.0)
        self.assertEqual(sum(series["gte8_count"]), 1)
        self.assertEqual(series["gpu_1_7_count"][0], 1)
        self.assertEqual(sum(series["cpu_only_count"]), 0)

    def test_wait_chart_splits_cpu_small_gpu_and_large_gpu_jobs(self):
        conn = sqlite3.connect(self.db)
        conn.executemany(
            """INSERT INTO jobs (
                job_id, user, job_name, state, start_time_ms, end_time_ms, submit_time_ms,
                req_gpus, req_cpus, last_seen_ms
            ) VALUES (?, 'bob', 't', 'COMPLETED', ?, ?, ?, ?, 1, ?)""",
            [
                ('cpu', self.from_ms + H, self.from_ms + 2 * H,
                 self.from_ms, 0, self.now_ms),
                ('small-gpu', self.from_ms + 2 * H, self.from_ms + 3 * H,
                 self.from_ms, 4, self.now_ms),
                ('large-gpu', self.from_ms + 3 * H, self.from_ms + 4 * H,
                 self.from_ms, 8, self.now_ms),
            ],
        )
        conn.commit()
        conn.close()

        data = history.get_wait_buckets(self.db, self.from_ms, self.to_ms)
        series = data["wait_time_buckets"]["bob"]
        self.assertEqual(sum(series["cpu_only_count"]), 1)
        self.assertEqual(sum(series["gpu_1_7_count"]), 1)
        self.assertEqual(sum(series["gte8_count"]), 1)
        self.assertAlmostEqual(sum(series["cpu_only_sum"]), 1.0)
        self.assertAlmostEqual(sum(series["gpu_1_7_sum"]), 2.0)
        self.assertAlmostEqual(sum(series["gte8_sum"]), 3.0)

    def test_unit_gets_half_bucket_not_zero(self):
        data = history.get_usage_buckets(self.db, self.from_ms, self.to_ms, group_by="unit")
        unit_h = _gpu_hours_from_buckets(data, "Golf Unit")
        self.assertAlmostEqual(unit_h, 12.0, places=1)
        unassigned_h = _gpu_hours_from_buckets(data, "Unassigned")
        self.assertAlmostEqual(unassigned_h, 12.0, places=1)

    def test_old_bucket_start_would_undercount(self):
        """Whole bucket at assignment start would credit 24h to unit (wrong)."""
        proj_map, unit_counts = {}, {}
        conn = sqlite3.connect(self.db)
        proj_map, unit_counts = history._fetch_proj_map(conn, self.from_ms, self.to_ms)
        conn.close()
        grouped = {"Golf Unit": [24.0]}  # fake: entire bucket value
        # Simulate mis-attribution at bucket start (assignment not active yet)
        key_at_start, _ = history._resolve_group_key("alice", self.from_ms, proj_map, "unit", unit_counts)
        self.assertEqual(key_at_start, "Unassigned")

    def test_job_name_rule_overrides_overlapping_time_assignment(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO research_units (id, title, color) VALUES (9, 'Molecule Unit', '#def')")
        conn.execute("INSERT INTO projects (id, title, unit_id, sort_order) VALUES (17, 'Molecule Project', 9, 0)")
        conn.execute(
            "INSERT INTO job_project_rules (project_id, user, match_type, pattern) VALUES (17, 'alice', 'prefix', 'mol-')"
        )
        conn.executemany(
            """INSERT INTO jobs (
                job_id, user, job_name, state, start_time_ms, end_time_ms, submit_time_ms,
                req_gpus, req_cpus, last_seen_ms
            ) VALUES (?, 'alice', ?, 'COMPLETED', ?, ?, ?, 1, 1, ?)""",
            [
                ('rule-job', 'mol-run', self.from_ms, self.to_ms,
                 self.from_ms, self.now_ms),
                ('time-job', 'other-run', self.from_ms, self.to_ms,
                 self.from_ms, self.now_ms),
            ],
        )
        conn.commit()
        conn.close()

        data = history.get_usage_buckets(self.db, self.from_ms, self.to_ms, group_by='project')
        self.assertAlmostEqual(_gpu_hours_from_buckets(data, 'Molecule Project'), 24.0, places=1)
        self.assertAlmostEqual(_gpu_hours_from_buckets(data, 'Golf Project'), 24.0, places=1)
        self.assertAlmostEqual(_gpu_hours_from_buckets(data, 'Unassigned'), 24.0, places=1)

    def test_exact_rule_does_not_match_longer_name(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO job_project_rules (project_id, user, match_type, pattern) VALUES (16, 'bob', 'exact', 'brick-s1')"
        )
        conn.executemany(
            """INSERT INTO jobs (
                job_id, user, job_name, state, start_time_ms, end_time_ms, submit_time_ms,
                req_gpus, req_cpus, last_seen_ms
            ) VALUES (?, 'bob', ?, 'COMPLETED', ?, ?, ?, 1, 1, ?)""",
            [
                ('exact', 'brick-s1', self.from_ms, self.from_ms + H,
                 self.from_ms, self.now_ms),
                ('longer', 'brick-s1-extra', self.from_ms, self.from_ms + H,
                 self.from_ms, self.now_ms),
            ],
        )
        conn.commit()
        conn.close()

        data = history.get_usage_buckets(self.db, self.from_ms, self.to_ms, group_by='project')
        self.assertAlmostEqual(_gpu_hours_from_buckets(data, 'Golf Project'), 13.0, places=1)
        self.assertAlmostEqual(_gpu_hours_from_buckets(data, 'Unassigned'), 13.0, places=1)

    def test_name_rule_removes_only_matching_jobs_from_unassigned(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO job_project_rules (project_id, user, match_type, pattern) VALUES (16, 'bob', 'prefix', 'mapped-')"
        )
        conn.executemany(
            """INSERT INTO jobs (
                job_id, user, job_name, state, start_time_ms, end_time_ms, submit_time_ms,
                req_gpus, req_cpus, last_seen_ms
            ) VALUES (?, 'bob', ?, 'COMPLETED', ?, ?, ?, 1, 1, ?)""",
            [
                ('mapped', 'mapped-run', self.from_ms, self.from_ms + H,
                 self.from_ms, self.now_ms),
                ('unmapped', 'other-run', self.from_ms, self.from_ms + H,
                 self.from_ms, self.now_ms),
            ],
        )
        conn.commit()
        conn.close()

        rows = {row['user']: row for row in history.get_unassigned_jobs(self.db)}
        self.assertEqual(rows['bob']['job_count'], 1)
        self.assertAlmostEqual(rows['bob']['gpu_hours'], 1.0)


@unittest.skipUnless(
    bool(os.environ.get("SLURM_PARAMETER_GOLF_TEST_DB")),
    "optional Parameter Golf fixture not configured",
)
class TestParameterGolfProduction(unittest.TestCase):
    def test_unit_hours_near_job_based_last_90_days(self):
        import time

        db = os.environ["SLURM_PARAMETER_GOLF_TEST_DB"]
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - 90 * 86400000
        to_ms = now_ms
        unit = "OpenAI Parameter Golf: Armenia"

        data = history.get_usage_buckets(db, from_ms, to_ms, group_by="unit")
        page_h = _gpu_hours_from_buckets(data, unit)
        sql_h = _sql_unit_gpu_hours(db, 8, from_ms, to_ms, now_ms)

        # Allow small integration tolerance (bucket edges); must be within 2% of SQL reference
        self.assertGreater(page_h, 700, msg=f"still undercounting badly: {page_h}")
        rel_err = abs(page_h - sql_h) / sql_h if sql_h else 0
        self.assertLess(rel_err, 0.02, msg=f"page={page_h} sql={sql_h} err={rel_err:.4f}")


if __name__ == "__main__":
    unittest.main()
