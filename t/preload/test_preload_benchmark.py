"""Check benchmark timeout cleanup with a descendant holding captured pipes."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "perf"))
import preload_pool_benchmark as benchmark


@unittest.skipUnless(os.name == "posix", "requires Unix process groups")
class TimeoutTests(unittest.TestCase):
    """Exercise cleanup using real processes rather than mocked kill calls."""

    def test_descendant_cannot_hold_capture_open_after_timeout(self):
        """A timed-out shell's descendant must not keep communicate blocked."""
        real_popen = subprocess.Popen

        class ShortTimeout(real_popen):
            """Use a short deadline for the same production cleanup path."""

            def communicate(self, input=None, timeout=None):
                """Limit waits to 200 ms so an unbounded drain fails this test."""
                return super().communicate(input, timeout=.2)

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-bash"
            child_file = Path(directory) / "child.pid"
            script.write_text(
                "#!/usr/bin/env python3\nimport os, time\n"
                "pid = os.fork()\n"
                f"if pid: open({str(child_file)!r}, 'w').write(str(pid))\n"
                "print('READY', flush=True)\ntime.sleep(60)\n")
            script.chmod(0o700)
            try:
                with patch.object(benchmark.subprocess, "Popen", ShortTimeout):
                    with self.assertRaisesRegex(AssertionError, "timeout"):
                        benchmark.run_case(str(script), "unused", "unused",
                                           os.environ.copy(), 3, 0, False)
            finally:
                if child_file.exists():
                    try:
                        os.kill(int(child_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
