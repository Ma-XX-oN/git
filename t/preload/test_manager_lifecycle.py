#!/usr/bin/env python3
"""Exercise real pool shutdown while worker startup has not registered its child."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch


class Worker:
    """Minimal subprocess fixture owned by the pool under test."""

    def __init__(self):
        """Start alive with the same READY stream shape as a native worker."""
        self.pid = 987654
        self.returncode = None
        self.stdout = io.BytesIO(f"READY {self.pid}\n".encode())

    def poll(self):
        """Return the fixture's process status, or None while alive."""
        return self.returncode

    def kill(self):
        """Record pool-initiated process termination."""
        self.returncode = -1

    def wait(self, timeout=None):
        """Return the terminated status without performing operating-system waits.

        Parameters
        ----------
        timeout : float, optional
            Pool-supplied deadline in seconds; fixture termination is immediate.

        Returns
        -------
        int
            Terminated status; an alive process indicates missing pool cleanup.
        """
        assert self.returncode is not None, "pool waited without terminating worker"
        return self.returncode


def check_close_during_spawn(manager, windows):
    """Hold process startup across the shutdown snapshot and check ownership.

    Parameters
    ----------
    manager : module
        Production pool implementation, loaded without starting its server.
    windows : bool
        Whether this module uses the Windows pool constructor.
    """
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    worker = Worker()
    failures = []

    def popen(*args, **kwargs):
        """Hold the supplied subprocess arguments until the test releases startup."""
        entered.set()
        assert release.wait(5), "fixture startup was not released"
        return worker

    with tempfile.TemporaryDirectory(prefix="preload-manager-lifecycle-") as directory:
        if windows:
            pool = manager.Pool(Path("unused"), 1, Path(directory))
            pool.owner_pid = os.getpid()
        else:
            pool = manager.Pool(Path("unused"), Path(directory), 1, os.getpid())

        def close():
            """Record shutdown completion or its exception for the main test thread."""
            try:
                pool.close()
            except Exception as error:
                failures.append(error)
            finally:
                finished.set()

        with patch.object(manager.subprocess, "Popen", side_effect=popen):
            starter = threading.Thread(target=pool.spawn)
            closer = threading.Thread(target=close)
            starter.start()
            try:
                assert entered.wait(5), "pool did not enter process startup"
                closer.start()
                assert not finished.wait(.05), "pool closed before in-flight startup completed"
            finally:
                release.set()
                starter.join(5)
                if closer.ident is not None:
                    closer.join(5)
            assert not starter.is_alive() and not closer.is_alive(), "pool shutdown stuck"
            assert not failures, failures
            assert worker.poll() is not None, "pool leaked a late-starting worker"
            assert worker.stdout.closed, "pool retained the startup stream"
    print("PRELOAD_MANAGER_LIFECYCLE_PASS", "windows" if windows else "posix", flush=True)


def check_unpublished_allocation(manager, windows):
    """Keep worker ownership until its allocation reply has been published.

    Parameters
    ----------
    manager : module
        Production pool backend under test.
    windows : bool
        Whether this backend uses Windows process entries and alloc().
    """
    worker = Worker()
    with tempfile.TemporaryDirectory(prefix="preload-unpublished-allocation-") as directory:
        if windows:
            pool = manager.Pool(Path("unused"), 1, Path(directory))
            pool.owner_pid = os.getpid()
            entry = worker
        else:
            pool = manager.Pool(Path("unused"), Path(directory), 1, os.getpid())
            entry = (worker, Path(directory) / f"worker-{worker.pid}")
        pool.children.append(entry)
        pool.ready.put(entry)
        try:
            answer = pool.alloc() if windows else pool.allocate()
            assert answer.strip() == str(worker.pid), answer
            # Simulate reply failure: the client has not received this PID.
            pool.close()
            assert worker.poll() is not None, "pool abandoned an unpublished allocation"
            assert worker.stdout.closed, "pool retained an unpublished worker's stream"
        finally:
            worker.kill()
            worker.stdout.close()
    print("PRELOAD_UNPUBLISHED_ALLOCATION_PASS", "windows" if windows else "posix", flush=True)


def main():
    """Run the startup/shutdown race against each locally importable backend."""
    root = Path(__file__).resolve().parents[2] / "contrib/preload"
    for backend in (("unix", False), ("windows", True)):
        name, windows = backend
        if windows and os.name != "nt":
            continue
        spec = importlib.util.spec_from_file_location(name + "_pool", root / (name + "_git_ipc_pool.py"))
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        check_close_during_spawn(manager, windows)
        check_unpublished_allocation(manager, windows)


if __name__ == "__main__":
    main()
