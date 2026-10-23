#!/usr/bin/env python3
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slurm


class ArrayTaskCountTest(unittest.TestCase):
    def test_plain_compact_range(self):
        self.assertEqual(slurm._array_task_count("232925_[0-35]"), 36)

    def test_throttled_compact_range_keeps_full_task_count(self):
        self.assertEqual(slurm._array_task_count("233562_[141-261%8]"), 121)

    def test_single_task(self):
        self.assertEqual(slurm._array_task_count("233562_140"), 1)


if __name__ == "__main__":
    unittest.main()
