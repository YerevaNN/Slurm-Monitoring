#!/usr/bin/env python3
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import history
import slurm


class NodeDiscoveryTest(unittest.TestCase):
    @patch("slurm._run")
    def test_discovers_each_nodes_capacity_and_deduplicates_partitions(self, run):
        run.return_value = "\n".join([
            "ap-rtx|24|gpu:rtx_a6000:1",
            "gpu01|224|gpu:h100:8,cpuonly:96",
            "gpu01|224|gpu:h100:8,cpuonly:96",
            "gpu04|224|gpu:h100:6,cpuonly:96",
        ])
        self.assertEqual(slurm.fetch_node_capacities(), {
            "ap-rtx": {"gpus": 1, "cpus": 24},
            "gpu01": {"gpus": 8, "cpus": 224},
            "gpu04": {"gpus": 6, "cpus": 224},
        })

    def test_gpu_nodes_sort_before_other_nodes(self):
        nodes = ["ap-rtx", "gpu09", "gpu02", "gpu01"]
        self.assertEqual(sorted(nodes, key=slurm._node_sort_key), [
            "gpu01", "gpu02", "gpu09", "ap-rtx",
        ])


class NodeCapacityPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        history.init_db(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_meta_and_usage_return_summed_capacity(self):
        capacities = {
            "gpu01": {"gpus": 8, "cpus": 224},
            "gpu04": {"gpus": 6, "cpus": 224},
            "ap-rtx": {"gpus": 1, "cpus": 24},
        }
        history.update_meta(
            self.tmp.name,
            ["gpu01", "gpu04", "ap-rtx"],
            {}, {}, 8, 224, 123, {}, capacities,
        )
        meta = history.get_latest_meta(self.tmp.name)
        self.assertEqual(meta["node_capacities"], capacities)
        self.assertEqual(meta["total_gpus"], 15)
        self.assertEqual(meta["total_cpus"], 472)

        usage = history.get_usage_buckets(self.tmp.name, 0, 3_600_000)
        self.assertEqual(usage["total_gpus"], 15)
        self.assertEqual(usage["total_cpus"], 472)


if __name__ == "__main__":
    unittest.main()
