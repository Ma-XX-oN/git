#!/usr/bin/env python3
"""Native pool lifecycle tests using the independently compiled C READY fixture.

Build preload-manager and preload/perf/ready-worker first. PRELOAD_MANAGER and
PRELOAD_READY_FIXTURE select alternate build outputs. No Python pool internals
are mocked; tests exercise the actual request protocol and native processes.
"""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from test_manager import Session, ROOT, WINDOWS

FIXTURE = Path(os.environ.get("PRELOAD_READY_FIXTURE", ROOT / "preload/perf" /
                             ("ready-worker.exe" if WINDOWS else "ready-worker"))).resolve()


class NativePools(unittest.TestCase):
    """Exercise real startup, replacement, allocation and shutdown ownership."""

    def setUp(self):
        """Create a private session using the requested native executable."""
        self.temporary = tempfile.TemporaryDirectory(prefix="preload-native-pool-")
        self.directory = Path(self.temporary.name)
        self.session = Session(self.directory)

    def tearDown(self):
        """Require orderly shutdown and no remaining endpoints or workers."""
        if self.session.process.poll() is None:
            self.session.stop()
        else:
            self.session.log.close()
        self.assertFalse((self.directory / "ready.json").exists())
        self.assertFalse(list(self.directory.glob("slot.*")))
        self.assertFalse(list(self.directory.glob("worker-*")))
        self.temporary.cleanup()

    def preload(self, name, executable=FIXTURE):
        """Register borrowed name/path; return protocol reply."""
        slot = "" if WINDOWS else "{slot} "
        return self.session.request(f"PRELOAD {name} {slot}{executable}")

    def allocate(self, name):
        """Allocate a READY-only fixture and terminate it after checking its PID."""
        reply = self.session.request(f"GET {name}" + ("" if WINDOWS else " {slot}"))
        self.assertIn("worker-", reply)
        pid = int(reply.rsplit("worker-", 1)[1])
        os.kill(pid, signal.SIGTERM)
        if not WINDOWS:
            self.session.send(f"DONE {pid}")
        return pid

    def test_multiple_non_git_commands(self):
        """One generic manager serves distinct registrations with distinct workers."""
        self.assertEqual(self.preload("alpha"), "OK")
        self.assertEqual(self.preload("beta"), "OK")
        self.assertNotEqual(self.allocate("alpha"), self.allocate("beta"))

    def test_failed_replacement_keeps_previous_pool(self):
        """A real unsupported executable must not discard the registered pool."""
        self.assertEqual(self.preload("probe"), "OK")
        unsupported = Path(os.environ["SystemRoot"]) / "System32/whoami.exe" if WINDOWS else Path("/bin/true")
        self.assertIn("doesn't support --preload-worker", self.preload("probe", unsupported))
        self.allocate("probe")

    def test_idle_worker_death(self):
        """Allocation skips a killed READY worker and replenishes capacity."""
        self.assertEqual(self.preload("probe"), "OK")
        ready = [int(line.split()[-1]) for line in self.session.read_log().splitlines()
                 if line.startswith("WORKER_READY probe ")]
        self.assertEqual(len(ready), 3)
        os.kill(ready[0], signal.SIGTERM)
        time.sleep(.05)
        self.assertNotEqual(self.allocate("probe"), ready[0])
        self.allocate("probe")

    def test_stop_during_startup(self):
        """STOP during real child startup leaves no live descendant or request thread."""
        self.session.stop()
        self.session = Session(self.directory, environment={**os.environ, "PRELOAD_FIXTURE_DELAY_MS": "500"})
        failures = []

        def register():
            """Capture cancellation/error reply without losing a test thread exception."""
            try:
                reply = self.preload("probe")
                if reply == "OK": failures.append("registration completed before cancellation")
            except (OSError, AssertionError):
                pass  # Shutdown may cancel the request before its error is delivered.

        thread = threading.Thread(target=register)
        thread.start()
        deadline = time.monotonic() + 5
        children = []
        while not children and time.monotonic() < deadline:
            children = [int(path.read_text()) for path in self.directory.glob("fixture-pid-*")
                        if path.stat().st_size]
            time.sleep(.005)
        self.assertTrue(children, self.session.read_log())
        self.session.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        for child in children:
            if WINDOWS:
                import psutil
                self.assertFalse(psutil.pid_exists(child))
            else:
                with self.assertRaises(ProcessLookupError): os.kill(child, 0)

    def test_ready_timeout(self):
        """A live executable without timely READY is killed within the support deadline."""
        self.session.stop()
        self.session = Session(self.directory, environment={**os.environ, "PRELOAD_FIXTURE_DELAY_MS": "5000"})
        started = time.monotonic()
        self.assertIn("doesn't support --preload-worker", self.preload("probe"))
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipIf(WINDOWS, "POSIX mailbox reclamation")
    def test_unread_allocation_is_killed_and_reaped(self):
        """A dead mailbox claimant must not leave an allocated worker zombie."""
        self.assertEqual(self.preload("probe"), "OK")
        dead = subprocess.Popen(["/bin/true"])
        dead.wait()
        self.session.request("GET probe {slot}", pid=dead.pid)
        deadline = time.monotonic() + 5
        worker = None
        while worker is None and time.monotonic() < deadline:
            allocations = [line for line in self.session.read_log().splitlines() if line.startswith("ALLOCATED probe ")]
            if allocations: worker = int(allocations[-1].split()[-1])
            time.sleep(.01)
        self.assertIsNotNone(worker)
        while time.monotonic() < deadline:
            try: os.kill(worker, 0)
            except ProcessLookupError: return
            time.sleep(.01)
        self.fail("unread allocation was not reaped")


if __name__ == "__main__":
    unittest.main()
