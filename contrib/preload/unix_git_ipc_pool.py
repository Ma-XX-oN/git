#!/usr/bin/env python3
"""Unix-only four-FIFO Git preload worker pool; no Git command output relay."""
import argparse
import errno
import json
import os
from pathlib import Path
import queue
import select
import signal
import subprocess
import threading
import time


class Pool:
    def __init__(self, git, directory, target, owner_pid):
        """Initialize owned resources and lifecycle state."""
        self.git = git
        self.directory = directory
        self.target = target
        self.owner_pid = owner_pid
        self.ready = queue.Queue()
        self.children = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.errors = []

    def spawn(self):
        """Start a single-use worker and publish it only after READY."""
        if self.stopping.is_set():
            return
        worker_env = os.environ.copy()
        worker_env["GIT_PRELOAD_OWNER_PID"] = str(self.owner_pid)
        p = subprocess.Popen(
            [str(self.git), "--preload-worker", str(self.directory)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=worker_env, bufsize=0)

        channel_dir = self.directory / ("worker-" + str(p.pid))
        with self.lock:
            self.children.append((p, channel_dir))
        line = p.stdout.readline()
        p.stdout.close()
        if line != ("READY " + str(p.pid) + "\n").encode("ascii"):
            self.errors.append("worker readiness failed " + repr(line))
            if p.poll() is None:
                p.kill()
            return
        if self.stopping.is_set():
            p.kill()
            return
        self.ready.put((p, channel_dir))
        print("WORKER_READY " + str(p.pid), flush=True)

    def allocate(self):
        """Transfer one READY worker to the requesting serial client."""
        try:
            p, channel_dir = self.ready.get(timeout=5)
        except queue.Empty:
            return "WAIT"
        with self.lock:
            self.children.remove((p, channel_dir))
        if p.poll() is not None:
            threading.Thread(target=self.spawn, daemon=True).start()
            return "WAIT"
        # Client owns this worker; the worker owns cleaning its FIFO directory.
        print("ALLOCATED " + str(p.pid), flush=True)
        return str(p.pid)

    def close(self):
        """Stop pending work and release resources owned by this instance."""
        self.stopping.set()
        with self.lock:
            children = list(self.children)
        for p, _ in children:
            if p.poll() is None:
                p.kill()
        for p, directory in children:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
            if p.stdout:
                p.stdout.close()
            for name in ("stdout", "stderr", "exit-code", "stdin", "param-env-cwd"):
                (directory / name).unlink(missing_ok=True)
            directory.rmdir() if directory.exists() else None


def owner_alive(pid):
    """Check whether the original owning process still exists."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def run(args):
    """Serve serial allocation requests until shutdown or owner death."""
    root = Path(args.directory).resolve(strict=True)
    git = Path(args.git).resolve(strict=True)
    pool = Pool(git, root, args.workers, args.owner_pid)
    request = root / "allocate-req"
    response = root / "allocate-reply"
    os.mkfifo(request, 0o600)
    os.mkfifo(response, 0o600)
    # A FIFO has one application-data direction. A separate local writer
    # prevents EOF/readability spin while no external request writer is open.
    # Unlike O_RDWR, this is portable to macOS and conforming POSIX FIFOs.
    req_fd = os.open(request, os.O_RDONLY | os.O_NONBLOCK)
    try:
        req_guard_fd = os.open(request, os.O_WRONLY | os.O_NONBLOCK)
    except BaseException:
        os.close(req_fd)
        raise
    incoming = bytearray()
    response_fd = None
    try:
        for _ in range(args.workers):
            pool.spawn()
        if pool.ready.qsize() != args.workers:
            raise RuntimeError("initial worker pool incomplete: " + repr(pool.errors))
        ready = root / "ready.json"
        temporary = root / "ready.tmp"
        temporary.write_text(json.dumps({"state": "READY", "workers": args.workers,
                             "manager_pid": os.getpid(), "owner_pid": args.owner_pid,
                             "worker_pids": [p.pid for p, _ in list(pool.ready.queue)]})
                             + "\n", encoding="utf-8")
        temporary.replace(ready)
        print("POOL_READY " + str(args.workers), flush=True)
        # Keep one writer open for the entire manager session. Reopening the
        # writer per request lets Bash's next read attach to the previous
        # writer before it closes, causing a spurious EOF on macOS.
        # The shell opens its persistent reader after observing ready.json.
        deadline = time.monotonic() + 5
        while response_fd is None:
            if not owner_alive(args.owner_pid):
                raise RuntimeError("owner exited before reply FIFO connection")
            try:
                response_fd = os.open(response, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("allocation reply FIFO connection timeout")
                time.sleep(0.005)
        print("ALLOC_REPLY_SESSION_READY", flush=True)
        while owner_alive(args.owner_pid) and not pool.errors:
            readable, _, _ = select.select([req_fd], [], [], 0.25)
            if not readable:
                continue
            data = os.read(req_fd, 256)
            if not data:
                continue
            incoming.extend(data)
            if len(incoming) > 1024:
                raise RuntimeError("invalid allocation frame")
            while b"\n" in incoming:
                line, _, remainder = incoming.partition(b"\n")
                incoming = bytearray(remainder)
                answer = pool.allocate() if line == b"GET" else "INVALID"
                payload = (answer + "\n").encode("utf-8")
                written = os.write(response_fd, payload)
                if written != len(payload):
                    raise RuntimeError("short FIFO allocation reply write")
                if answer not in ("WAIT", "INVALID"):
                    # Publish the selected worker before replacement startup
                    # competes with the client's command on the critical path.
                    threading.Thread(target=pool.spawn, daemon=True).start()
        if pool.errors:
            raise RuntimeError("worker initialization failure: " + repr(pool.errors))
    finally:
        ready.unlink(missing_ok=True) if "ready" in locals() else None
        if response_fd is not None:
            os.close(response_fd)
        os.close(req_guard_fd)
        os.close(req_fd)
        pool.close()
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def main():
    """Parse command-line options and run the requested validation or service."""
    p = argparse.ArgumentParser()
    p.add_argument("--git", required=True)
    p.add_argument("--workers", type=int, required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--owner-pid", type=int, required=True)
    args = p.parse_args()
    if not 1 <= args.workers <= 32:
        p.error("workers must be 1 to 32")
    if not 1 <= args.owner_pid:
        p.error("invalid owner PID")
    run(args)


if __name__ == "__main__":
    main()
