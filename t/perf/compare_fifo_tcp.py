#!/usr/bin/env python3
"""Compare persistent Bash FIFO/TCP request-reply latency on Unix.

Run with --output FILE. Every validated RTT is retained in microseconds.
FIFO endpoints and TCP connections are opened before warmup and timing.
This isolates communication; it excludes mailbox creation and Git execution.
"""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading


CLIENT = r'''
set -e
set +o posix
if [[ $1 == fifo ]]; then
  exec {rx}<"$2" {tx}>"$3"
else
  exec {rx}<>"/dev/tcp/127.0.0.1/$2"
  tx=$rx
fi
expected=$4
[[ $5 != fixed9 ]] || expected+=$'\n'
for ((i=0; i<$6+$7; i++)); do
  start=$EPOCHREALTIME
  printf 'GET\n' >&"$tx"
  case $5 in
    line) IFS= read -r -t 10 -u "$rx" reply ;;
    fixed8) IFS= read -r -t 10 -N 8 -u "$rx" reply ;;
    fixed9) IFS= read -r -t 10 -N 9 -u "$rx" reply ;;
  esac
  end=$EPOCHREALTIME
  [[ $reply == "$expected" ]] || exit 42
  if ((i >= $7)); then printf '%s %s\n' "$start" "$end"; fi
done
'''


def serve(read_fd, write_fd, reply, count):
    """Validate count requests and write replies using common POSIX I/O.

    Parameters
    ----------
    read_fd, write_fd : int
        Borrowed blocking descriptors; the caller owns their lifetime.
    reply : bytes
        Complete ASCII response frame.
    count : int
        Number of exchanges, including warmup.
    """
    pending = b""
    for _ in range(count):
        while b"\n" not in pending:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                raise EOFError("client closed before all requests")
            pending += chunk
        request, pending = pending.split(b"\n", 1)
        if request != b"GET":
            raise ValueError(f"invalid request: {request!r}")
        remaining = memoryview(reply)
        while remaining:
            written = os.write(write_fd, remaining)
            if not written:
                raise IOError("zero-length reply write")
            remaining = remaining[written:]


def measure(transport, framing, count, warmup, bash):
    """Return validated raw RTT samples for one persistent connection.

    Parameters
    ----------
    transport : str
        fifo, tcp, or tcp-nodelay (server-side TCP_NODELAY).
    framing : str
        line, fixed8, or fixed9; fixed9 matches line's exact wire bytes.
    count, warmup : int
        Measured and discarded exchange counts respectively.
    bash : str
        Bash executable with EPOCHREALTIME support.

    Returns
    -------
    dict
        Transport, framing, median/P95 and every RTT in microseconds.
    """
    expected = f"{os.getpid():08x}"
    if len(expected) != 8:
        raise ValueError("PID exceeds eight hexadecimal digits")
    reply = (expected + ("" if framing == "fixed8" else "\n")).encode()
    errors = []
    with tempfile.TemporaryDirectory(prefix="fifo-tcp-") as directory:
        descriptors = []
        listener = None
        if transport == "fifo":
            request = str(Path(directory) / "request")
            response = str(Path(directory) / "response")
            os.mkfifo(request, 0o600)
            os.mkfifo(response, 0o600)
            # O_RDWR guards prevent startup ordering deadlocks on both Unix OSes.
            reader = os.open(request, os.O_RDWR)
            writer = os.open(response, os.O_RDWR)
            descriptors.extend([reader, writer])
            endpoint = ["fifo", response, request]
        else:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(15)
            endpoint = ["tcp", str(listener.getsockname()[1]), "unused"]

        def server():
            """Serve one client and preserve server exceptions for the caller."""
            try:
                if listener is None:
                    serve(reader, writer, reply, count + warmup)
                else:
                    with listener.accept()[0] as connection:
                        connection.settimeout(None)
                        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY,
                                              int(transport == "tcp-nodelay"))
                        serve(connection.fileno(), connection.fileno(), reply,
                              count + warmup)
            except Exception as error:
                errors.append(repr(error))

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        env = dict(os.environ, LC_ALL="C")
        for name in ("POSIXLY_CORRECT", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
            env.pop(name, None)
        try:
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", CLIENT, "benchmark",
                 *endpoint, expected, framing, str(count), str(warmup)],
                env=env, capture_output=True, text=True, timeout=60, check=True)
            thread.join(10)
            if thread.is_alive() or errors:
                raise RuntimeError(f"server incomplete: {errors}")
        finally:
            if listener is not None:
                listener.close()
            for descriptor in descriptors:
                os.close(descriptor)
        samples = [float((Decimal(end) - Decimal(start)) * 1000000)
                   for start, end in (line.split() for line in result.stdout.splitlines())]
        if len(samples) != count or any(value < 0 for value in samples):
            raise ValueError("missing samples or wall-clock reversal")
        return dict(transport=transport, framing=framing, samples_us=samples,
                    median_us=statistics.median(samples),
                    p95_us=sorted(samples)[int(.95 * (count - 1))])


def main():
    """Write matched ABBA rounds, machine metadata and raw samples as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if os.name != "posix" or args.count < 1 or args.rounds < 1:
        parser.error("requires Unix and positive count/rounds")
    bash = shutil.which("bash")
    hardware_command = (["sysctl", "hw.model", "machdep.cpu.brand_string", "hw.memsize"]
                        if platform.system() == "Darwin" else ["lscpu"])
    data = dict(platform=platform.platform(), machine=platform.machine(),
                hardware=subprocess.check_output(hardware_command, text=True),
                cpu_count=os.cpu_count(), python=platform.python_version(),
                bash=subprocess.check_output([bash, "--version"], text=True),
                source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                count=args.count, warmup=30, rounds=args.rounds,
                lifecycle="persistent; connection setup excluded",
                tcp_nodelay="server only; Bash client uses default socket options",
                results=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for framing in ("line", "fixed8", "fixed9"):
        for tcp in ("tcp", "tcp-nodelay"):
            for round_number in range(args.rounds):
                for transport in ("fifo", tcp, tcp, "fifo"):
                    row = measure(transport, framing, args.count, 30, bash)
                    row.update(round=round_number, comparison=tcp)
                    data["results"].append(row)
                    print(f"{framing} {tcp} round={round_number} {transport}: "
                          f"{row['median_us']} us", flush=True)
                    args.output.write_text(json.dumps(data, indent=2) + "\n",
                                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
