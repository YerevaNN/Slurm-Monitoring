import unittest
from unittest.mock import patch

import slurm


class PartitionMembershipTests(unittest.TestCase):
    @patch('slurm._expand_nodelist', return_value=['node01', 'node02'])
    @patch('slurm._run')
    def test_multiline_membership_and_priorities(self, run, expand):
        run.return_value = ('PartitionName=batch\n   AllocNodes=ALL Nodes=node[01-02]\n'
                            '   PriorityJobFactor=2 PriorityTier=10\n'
                            'PartitionName=interactive\n   Nodes=node[01-02]\n')
        result = slurm.fetch_partitions()
        self.assertEqual(result['batch'], dict(nodes=['node01', 'node02'],
                                              priority_tier=10, job_factor=2))
        self.assertEqual(result['interactive']['nodes'], ['node01', 'node02'])
        expand.assert_called_once_with('node[01-02]')

    @patch('slurm._run', return_value='PartitionName=cpu Nodes=node03 PriorityTier=5 PriorityJobFactor=3')
    def test_single_line_output(self, run):
        self.assertEqual(slurm.fetch_partitions()['cpu'],
                         dict(nodes=['node03'], priority_tier=5, job_factor=3))

    @patch('slurm._run', return_value='PartitionName=empty\n   AllocNodes=ALL Nodes=(null)\n')
    def test_unconfigured_partition_has_no_nodes(self, run):
        self.assertEqual(slurm.fetch_partitions()['empty']['nodes'], [])
