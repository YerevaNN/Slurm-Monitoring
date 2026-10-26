import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SLURM_DASHBOARD_DISABLE_RECORDER"] = "1"
import history
from gpu_metrics import parse_allocations, gres_minors, parse_nvidia_xml

LINE = ('JobId=1217 ArrayJobId=1216 ArrayTaskId=0 JobState=RUNNING '
        'SubmitTime=2026-10-26T15:07:41 StartTime=2026-10-26T15:07:41 Restarts=0 '
        'Nodes=b200-8 CPU_IDs=0-15 Mem=65536 GRES=gpu:b200:2(IDX:0,2) MinCPUsNode=16')


class MappingTests(unittest.TestCase):
    def test_array_identity_and_gres_order_are_not_cuda_device_order(self):
        minors = gres_minors('NodeName=b200-8 Name=gpu File=/dev/nvidia[3,1,7]', 'b200-8')
        jobs = parse_allocations(LINE, 'b200-8', minors)
        self.assertEqual(jobs[0]['job_id'], '1216_0')
        self.assertEqual(jobs[0]['minors'], [3, 7])

    def test_ysu_ranges_and_node_local_gres_config(self):
        config = 'NodeName=gpu[01-03,05-08] Name=gpu File=/dev/nvidia[0-7]'
        self.assertEqual(gres_minors(config, 'gpu02'), list(range(8)))
        with self.assertRaises(ValueError):
            gres_minors(config, 'gpu04')
        self.assertEqual(gres_minors('Name=gpu File=/dev/nvidia3', 'ap-rtx'), [3])
        grouped = LINE.replace('Nodes=b200-8', 'Nodes=gpu[01-03]')
        self.assertEqual(parse_allocations(grouped, 'gpu02', list(range(8)))[0]['minors'], [0,2])

    def test_ambiguous_ownership_and_other_nodes_are_excluded(self):
        self.assertEqual(parse_allocations(LINE + '\n' + LINE.replace('ArrayTaskId=0', 'ArrayTaskId=1'), 'b200-8', list(range(8))), [])
        self.assertEqual(parse_allocations(LINE, 'b200-8c', list(range(8))), [])

    def test_xml_unknown_counters_are_not_zero_and_mig_is_not_attributed(self):
        xml = '''<nvidia_smi_log><gpu><uuid>GPU-a</uuid><minor_number>3</minor_number>
            <utilization><gpu_util>N/A</gpu_util><memory_util>12 %</memory_util></utilization>
            <fb_memory_usage><used>50 MiB</used><total>200 MiB</total></fb_memory_usage></gpu>
            <gpu><uuid>GPU-b</uuid><minor_number>4</minor_number><mig_mode><current_mig>Enabled</current_mig></mig_mode></gpu></nvidia_smi_log>'''
        devices = parse_nvidia_xml(xml)
        self.assertIsNone(devices[3]['gpu'])
        self.assertEqual(devices[3]['vram'], 25)
        self.assertEqual(devices[3]['memory'], 12)
        self.assertNotIn(4, devices)

    def test_reallocation_during_query_is_discarded(self):
        from scripts.gpu_node_sampler import sample
        xml = '<nvidia_smi_log><gpu><uuid>GPU-a</uuid><minor_number>0</minor_number></gpu></nvidia_smi_log>'
        with patch('scripts.gpu_node_sampler.run', side_effect=[LINE, xml, LINE.replace('JobState=RUNNING', 'JobState=COMPLETED')]), patch('scripts.gpu_node_sampler.Path.read_text', return_value='NodeName=b200-8 Name=gpu File=/dev/nvidia[0-7]'):
            self.assertEqual(sample('b200-8', '/etc/slurm/gres.conf')['jobs'], [])

    def test_controller_mode_needs_no_local_slurm_command_and_checks_reallocation(self):
        from scripts.gpu_node_sampler import sample
        xml = '<nvidia_smi_log><gpu><uuid>GPU-a</uuid><minor_number>3</minor_number></gpu></nvidia_smi_log>'
        allocations = [{'job_id':'42','submit_time_ms':1000,'start_time_ms':1000,'restarts':0,'gpu_indices':[0]}]
        fetch = unittest.mock.Mock(side_effect=[allocations, allocations])
        with patch('scripts.gpu_node_sampler.run', return_value=xml) as run, patch('scripts.gpu_node_sampler.Path.read_text', return_value='Name=gpu File=/dev/nvidia3'):
            data = sample('ap-rtx', '/etc/slurm/gres.conf', fetch)
        run.assert_called_once_with(['nvidia-smi','-q','-x'])
        self.assertEqual(data['jobs'][0]['gpus'][0]['minor'], 3)
        self.assertEqual(fetch.call_count, 2)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.folder.name, 'gpu.db')
        history.init_db(self.db)
        self.job = dict(job_id='1216_0', submit_time=1_000_000, start_time=1_001_000,
                        state='RUNNING', req_gpus=2, allocations=[{'node':'b200-8','gpus':2}])
        history.upsert_jobs(self.db, [self.job], 1_015_000)

    def tearDown(self):
        self.folder.cleanup()

    def payload(self, ts, gpu=20):
        return dict(node='b200-8', timestamp_ms=ts, jobs=[dict(job_id='1216_0',
            submit_time_ms=1_000_000, start_time_ms=1_001_000,
            gpus=[dict(uuid='GPU-a', minor=0, gpu=gpu, vram=50, memory=10, used_mib=100, total_mib=200),
                  dict(uuid='GPU-b', minor=1, gpu=80, vram=75, memory=None, used_mib=150, total_mib=200)])])

    def result(self, now):
        return history.get_gpu_metrics(self.db, [copy.deepcopy(self.job)], now)[0]['gpu_telemetry']

    def test_welford_lifetime_survives_raw_pruning_and_unknowns_have_own_counts(self):
        history.ingest_gpu_sample(self.db, self.payload(1_015_000), 1_015_000)
        history.ingest_gpu_sample(self.db, self.payload(1_030_000, 40), 1_030_000)
        summary = self.result(1_030_000)
        self.assertAlmostEqual(summary['lifetime']['gpu']['mean'], 55)
        self.assertAlmostEqual(summary['lifetime']['gpu']['std'], math.sqrt(675))
        self.assertEqual(summary['lifetime']['memory']['n'], 2)
        self.assertEqual(summary['lifetime']['used_mib']['peak'], 150)
        self.assertEqual(summary['fresh_gpus'], 2)
        self.assertTrue(history.get_gpu_trend(self.db, '1216_0', 1_000_000)['raw_retained'])
        self.job.update(state='COMPLETED', end_time=1_031_000)
        history.upsert_jobs(self.db, [self.job], 1_032_000)
        history.cleanup_gpu_samples(self.db, 1_031_000 + 86_400_001)
        self.assertFalse(history.get_gpu_trend(self.db, '1216_0', 1_000_000)['raw_retained'])
        self.assertEqual(self.result(1_031_000 + 86_400_001)['lifetime']['gpu']['mean'], 55)

    def test_duplicate_replay_and_out_of_order_samples_do_not_inflate_summary(self):
        payload = self.payload(1_030_000)
        self.assertEqual(history.ingest_gpu_sample(self.db, payload, 1_030_000), 2)
        self.assertEqual(history.ingest_gpu_sample(self.db, payload, 1_031_000), 0)
        self.assertEqual(history.ingest_gpu_sample(self.db, self.payload(1_015_000), 1_031_000), 0)
        self.assertEqual(self.result(1_031_000)['lifetime']['gpu']['n'], 2)

    def test_unavailable_is_stale_not_zero_and_running_samples_are_not_pruned(self):
        history.ingest_gpu_sample(self.db, self.payload(1_015_000), 1_015_000)
        history.cleanup_gpu_samples(self.db, 1_015_000 + 100_000_000)
        self.assertTrue(history.get_gpu_trend(self.db, '1216_0', 1_000_000)['raw_retained'])
        summary = self.result(1_100_000)
        self.assertIsNone(summary['current']['gpu'])
        self.assertEqual(summary['lifetime']['gpu']['mean'], 50)

    def test_invalid_counter_rolls_back_whole_payload_and_job_reuse_cannot_inherit_stats(self):
        payload = self.payload(1_015_000)
        payload['jobs'][0]['gpus'][1]['gpu'] = float('nan')
        with self.assertRaises(ValueError):
            history.ingest_gpu_sample(self.db, payload, 1_015_000)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM gpu_samples').fetchone()[0], 0)
        history.ingest_gpu_sample(self.db, self.payload(1_030_000), 1_030_000)
        different = {**self.job, 'submit_time':2_000_000}
        self.assertNotIn('gpu_telemetry', history.get_gpu_metrics(self.db, [different], 2_000_001)[0])

    def test_stale_future_and_wrong_allocation_are_rejected(self):
        with self.assertRaises(ValueError):
            history.ingest_gpu_sample(self.db, self.payload(1_015_000), 2_000_000)
        with self.assertRaises(ValueError):
            history.ingest_gpu_sample(self.db, self.payload(2_000_000), 1_015_000)
        payload = self.payload(1_015_000)
        payload['jobs'][0]['gpus'].pop()
        with self.assertRaises(ValueError):
            history.ingest_gpu_sample(self.db, payload, 1_015_000)


class EndpointTests(unittest.TestCase):
    def test_controller_allocations_are_authenticated_and_return_only_target_node(self):
        import app
        client = app.app.test_client()
        with patch('app.gpu_ingest_token', return_value='private-collector-token'), patch('app.subprocess.run') as run:
            run.return_value.stdout = LINE
            self.assertEqual(client.get('/api/gpu-allocations/b200-8').status_code, 401)
            run.assert_not_called()
            response = client.get('/api/gpu-allocations/b200-8', headers={'X-GPU-Monitor-Token':'private-collector-token'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['jobs'][0]['gpu_indices'], [0,2])
            self.assertNotIn('minors', response.get_json()['jobs'][0])

    def test_ingest_requires_dedicated_token_and_valid_payload(self):
        import app
        client = app.app.test_client()
        with patch('app.gpu_ingest_token', return_value='private-collector-token'), patch('app.history.ingest_gpu_sample', return_value=2) as ingest:
            self.assertEqual(client.post('/api/gpu-samples', json={}).status_code, 401)
            ingest.assert_not_called()
            self.assertEqual(client.post('/api/gpu-samples', json={}, headers={'X-GPU-Monitor-Token':'private-collector-token'}).status_code, 200)
            self.assertEqual(client.post('/api/gpu-samples', data='x'*131073, headers={'X-GPU-Monitor-Token':'private-collector-token'}).status_code, 413)


if __name__ == '__main__':
    unittest.main()
