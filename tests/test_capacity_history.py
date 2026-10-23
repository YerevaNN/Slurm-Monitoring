import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history

MINUTE = 60_000


class CapacityHistoryTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.folder.name, 'history.db')
        history.init_db(self.db)

    def tearDown(self):
        self.folder.cleanup()

    def snapshot(self, timestamp, capacities, states=None):
        history.update_meta(self.db, list(capacities), states or {}, {}, 8, 128,
                            timestamp, {}, capacities)

    def job(self, end=15 * MINUTE):
        history.upsert_jobs(self.db, [dict(job_id='1', submit_time=0, start_time=0,
            end_time=end, state='COMPLETED', user='alice', req_gpus=12, req_cpus=192)], end)

    def test_growth_and_partial_bucket_integrate_exact_gpu_and_cpu_hours(self):
        self.snapshot(0, {'a': {'gpus': 12, 'cpus': 192}})
        self.snapshot(5 * MINUTE, {'a': {'gpus': 12, 'cpus': 192},
                                   'b': {'gpus': 4, 'cpus': 64}})
        self.job()
        d = history.get_usage_buckets(self.db, 0, 15 * MINUTE)
        self.assertEqual(d['bucket_durations_ms'], [10 * MINUTE, 5 * MINUTE])
        self.assertEqual(d['capacity_buckets']['gpus'], [14, 16])
        self.assertAlmostEqual(d['installed_gpu_hours'], 12 * 5 / 60 + 16 * 10 / 60)
        self.assertAlmostEqual(d['installed_cpu_hours'], d['installed_gpu_hours'] * 16)
        self.assertEqual(d['users']['alice'], [12, 12])
        used = sum(v * t / 3_600_000 for v, t in zip(d['users']['alice'], d['bucket_durations_ms']))
        self.assertAlmostEqual(used, 3)
        self.assertAlmostEqual(used / d['installed_gpu_hours'], 9 / 11)
        self.assertTrue(d['capacity_coverage_complete'])

    def test_drain_and_down_are_separate_and_do_not_reduce_denominator(self):
        caps = {'a': {'gpus': 4, 'cpus': 64}, 'b': {'gpus': 8, 'cpus': 128}}
        self.snapshot(0, caps)
        self.snapshot(5 * MINUTE, caps, {'a': 'DRNG', 'b': 'DOWN*'})
        self.snapshot(10 * MINUTE, caps, {'a': 'IDLE', 'b': 'MIX'})
        d = history.get_usage_buckets(self.db, 0, 15 * MINUTE)
        self.assertEqual(d['capacity_buckets']['gpus'], [12, 12])
        self.assertEqual(d['capacity_buckets']['drained_gpus'], [2, 0])
        self.assertEqual(d['capacity_buckets']['down_gpus'], [4, 0])
        self.assertAlmostEqual(d['installed_gpu_hours'], 3)

    def test_repeated_polls_and_idle_to_mixed_do_not_grow_history(self):
        caps = {'a': {'gpus': 4, 'cpus': 64}}
        self.snapshot(0, caps, {'a': 'IDLE'})
        self.snapshot(MINUTE, caps, {'a': 'MIX'})
        self.snapshot(2 * MINUTE, caps, {'a': 'IDLE'})
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM capacity_history').fetchone()[0], 1)

    def test_unknown_history_is_not_filled_with_current_capacity(self):
        self.snapshot(5 * MINUTE, {'a': {'gpus': 32, 'cpus': 512}})
        d = history.get_usage_buckets(self.db, 0, 15 * MINUTE)
        self.assertFalse(d['capacity_coverage_complete'])
        self.assertIsNone(d['capacity_points'][0]['gpus'])
        self.assertIsNone(d['capacity_buckets']['gpus'][0])
        self.assertEqual(d['capacity_buckets']['gpus'][1], 32)
        self.assertEqual(d['capacity_known_from_ms'], 5 * MINUTE)

    def test_boundary_changes_and_decommissioning(self):
        self.snapshot(0, {'a': {'gpus': 8, 'cpus': 128}, 'b': {'gpus': 4, 'cpus': 64}})
        self.snapshot(10 * MINUTE, {'b': {'gpus': 4, 'cpus': 64}})
        d = history.get_usage_buckets(self.db, 0, 20 * MINUTE)
        self.assertEqual(d['capacity_buckets']['gpus'], [12, 4])
        self.assertAlmostEqual(d['installed_gpu_hours'], 16 / 6)
        d = history.get_usage_buckets(self.db, 10 * MINUTE, 20 * MINUTE)
        self.assertEqual(d['capacity_buckets']['gpus'], [4])

    def test_future_and_partial_bucket_clip_both_usage_and_capacity(self):
        self.snapshot(0, {'a': {'gpus': 12, 'cpus': 192}})
        self.job(end=30 * MINUTE)
        with patch('time.time', return_value=15 * MINUTE / 1000):
            d = history.get_usage_buckets(self.db, 0, 30 * MINUTE)
        self.assertEqual(d['to_ms'], 15 * MINUTE)
        self.assertEqual(d['bucket_durations_ms'], [10 * MINUTE, 5 * MINUTE])
        self.assertEqual(d['users']['alice'], [12, 12])
        self.assertAlmostEqual(d['installed_gpu_hours'], 3)

    def test_grouped_hours_share_exact_partial_bucket_normalization(self):
        self.snapshot(0, {'a': {'gpus': 12, 'cpus': 192}})
        self.job()
        for group in ('user', 'project', 'unit'):
            d = history.get_usage_buckets(self.db, 0, 15 * MINUTE, group)
            hours = sum(sum(v * t / 3_600_000 for v, t in zip(values, d['bucket_durations_ms']))
                        for values in d['users'].values())
            self.assertAlmostEqual(hours, 3)
            self.assertAlmostEqual(d['installed_gpu_hours'], 3)

    def test_legacy_migration_starts_at_last_observation(self):
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO meta (id, updated_at_ms, node_capacities_json) VALUES (1, ?, ?)',
                      (5 * MINUTE, json.dumps({'a': {'gpus': 4, 'cpus': 64}})))
        history.init_db(self.db)
        history.init_db(self.db)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT timestamp_ms, source FROM capacity_history').fetchall(),
                             [(5 * MINUTE, 'legacy_metadata')])

    def test_import_is_idempotent_and_conflicting_batch_rolls_back(self):
        item = dict(timestamp_ms=0, node_capacities={'a': {'gpus': 4, 'cpus': 64}}, source='evidence')
        history.import_capacity_history(self.db, [item])
        history.import_capacity_history(self.db, [item])
        with self.assertRaises(ValueError):
            history.import_capacity_history(self.db, [
                dict(item, timestamp_ms=MINUTE),
                dict(item, timestamp_ms=2 * MINUTE, node_capacities={'a': {'gpus': -1, 'cpus': 64}})])
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM capacity_history').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
