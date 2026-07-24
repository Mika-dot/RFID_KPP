from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from common.single_instance import SingleInstanceError, SingleInstanceLock


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "service.lock")
            first = SingleInstanceLock(path)
            try:
                with self.assertRaises(SingleInstanceError):
                    SingleInstanceLock(path)
            finally:
                first.close()


if __name__ == "__main__":
    unittest.main()
