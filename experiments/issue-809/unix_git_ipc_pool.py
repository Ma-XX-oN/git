#!/usr/bin/env python3
"""Unix-only four-FIFO Git #809 worker pool; no Git command output relay."""
import argparse
import json
import os
from pathlib import Path
import queue
import select
import signal
import subprocess
import threading
import time
import uuid


class Pool:
    def __init__(self, git, directory, target):
        self.git = git
        self.directory = directory
        self.target = target
        self.ready = queue.Queue()
        self.children = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.errors = []

    def spawn(self):
        if self.stopping.is_set():
            return
        channel_dir = self.directory / ("worker-" + uuid.uuid4().hex)
        channel_dir.mkdir(mode=0o700)
        for name in ("stdout", "stderr", "exit-code", "stdin"):
            os.mkfifo(channel_dir / name, 0o600)
        p = subprocess.Popen(
            [str(self.git), "--git809-unix-worker", str(channel_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        with self.lock:
            self.children.append((p, channel_dir))
        line = p.stdout.readline()
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
        try:
            p, channel_dir = self.ready.get(timeout=5)
        except queue.Empty:
            return "WAIT"
        if p.poll() is not None:
            threading.Thread(target=self.spawn, daemon=True).start()
            return "WAIT"
        threading.Thread(target=self.spawn, daemon=True).start()
        print("ALLOCATED " + str(p.pid), flush=True)
        return str(channel_dir)

    def close(self):
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
            for name in ("stdout", "stderr", "exit-code", "stdin"):
                (directory / name).unlink(missing_ok=True)
            directory.rmdir()


def owner_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def run(args):
    root = Path(args.directory).resolve(strict=True)
    git = Path(args.git).resolve(strict=True)
    pool = Pool(git, root, args.workers)
    request = root / "allocate-req"
    response = root / "allocate-reply"
    os.mkfifo(request, 0o600)
    os.mkfifo(response, 0o600)
    req_fd = os.open(request, os.O_RDWR | os.O_NONBLOCK)
    incoming = bytearray()
    try:
        for _ in range(args.workers):
            pool.spawn()
        if pool.ready.qsize() != args.workers:
            raise RuntimeError("initial worker pool incomplete: " + repr(pool.errors))
        ready = root / "ready.json"
        ready.write_text(json.dumps({"state": "READY", "workers": args.workers,
                         "manager_pid": os.getpid()}) + "\n", encoding="utf-8")
        print("POOL_READY " + str(args.workers), flush=True)
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
                fd = os.open(response, os.O_WRONLY)
                try:
                    os.write(fd, (answer + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
        if pool.errors:
            raise RuntimeError("worker initialization failure: " + repr(pool.errors))
    finally:
        ready.unlink(missing_ok=True) if "ready" in locals() else None
        os.close(req_fd)
        pool.close()
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def main():
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
