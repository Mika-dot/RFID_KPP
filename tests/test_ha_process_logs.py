import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from guardian.processes import Processes


class ProcessLogTests(unittest.TestCase):
    def test_continuous_output_is_bounded_and_latest_event_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"RfidReader.log"
            manager=Processes({"state_dir":folder,"child_log_bytes":4096})
            content=(b"normal reader event\n"*10000)+b"latest-event\n"
            process=Mock(stdout=io.BytesIO(content))
            manager.capture(process,path)
            files=list(Path(folder).glob("RfidReader.log*"))
            self.assertLessEqual(len(files),4)
            self.assertLessEqual(sum(f.stat().st_size for f in files),4*4096)
            self.assertIn("latest-event",path.read_text())
