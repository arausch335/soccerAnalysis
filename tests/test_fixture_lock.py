import tempfile
import unittest
from fixtures.locking import output_lock


class FixtureLockTests(unittest.TestCase):
    def test_prevents_simultaneous_collectors_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            with output_lock(folder):
                with self.assertRaises(BlockingIOError):
                    with output_lock(folder):
                        pass
            with output_lock(folder):
                pass
