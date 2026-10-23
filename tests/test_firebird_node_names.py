import unittest
from unittest.mock import patch
import slurm

class SlurmNodeAliasTest(unittest.TestCase):
    @patch('slurm._run')
    def test_discovery_uses_job_allocation_names_not_vm_hostnames(self, run):
        # %N is NodeName, matching squeue allocations. %n is NodeHostName.
        outputs = {
            '%N': 'b200-4\nb200-8',
            '%N|%t|%E': 'b200-4|mix|\nb200-8|mix|',
            '%N|%c|%G': 'b200-4|64|gpu:b200:4\nb200-8|128|gpu:b200:8',
        }
        run.side_effect = lambda cmd: outputs[cmd[-1]]
        self.assertEqual(slurm.fetch_nodes(), ['b200-4', 'b200-8'])
        self.assertEqual(set(slurm.fetch_node_states()[0]), {'b200-4', 'b200-8'})
        self.assertEqual(slurm.fetch_node_capacities(), {
            'b200-4': {'cpus': 64, 'gpus': 4},
            'b200-8': {'cpus': 128, 'gpus': 8},
        })
