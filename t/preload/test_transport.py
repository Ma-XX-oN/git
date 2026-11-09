#!/usr/bin/env python3
"""Exercise the actual linked two-channel transport through test-tool."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


def main():
    """Connect native channels, consume markers and check payload ownership."""
    helper = str(Path(sys.argv[1]).resolve())
    if os.name == "nt":
        from test_windows_worker_lifecycle import K, INVALID, write, read_to_eof
        for winners in ((0, 0), (0, 1), (1, 0), (1, 1)):
            stem = r"\\.\pipe\git-preload-helper-" + uuid.uuid4().hex
            process = subprocess.Popen([helper, "preload", "transport", stem], stdout=subprocess.PIPE)
            handles = []
            try:
                assert process.stdout.readline() == b"READY\n"
                accepted = []
                for channel, winner in zip(("request", "reply"), winners):
                    pair = [K.CreateFileW(stem + "-" + channel, 0xc0000000, 0, None, 3, 0, None)
                            for _ in range(2)]
                    assert INVALID not in pair
                    handles.extend(pair)
                    accepted.append(pair[winner])
                    write(pair[winner], b"0")
                write(accepted[0], b"payload")
                assert read_to_eof(accepted[1]) == b"payload"
                assert process.wait(timeout=5) == 0
            finally:
                for handle in handles:
                    K.CloseHandle(handle)
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()
    else:
        with tempfile.TemporaryDirectory(prefix="preload-transport-") as directory:
            for name in ("request", "reply"):
                os.mkfifo(Path(directory) / name)
            process = subprocess.Popen([helper, "preload", "transport", directory], stdout=subprocess.PIPE)
            try:
                assert process.stdout.readline() == b"READY\n"
                with open(Path(directory) / "request", "wb", buffering=0) as request:
                    with open(Path(directory) / "reply", "rb", buffering=0) as reply:
                        request.write(b"payload")
                        assert reply.read() == b"payload"
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()
    print("PRELOAD_TRANSPORT_PASS", flush=True)


if __name__ == "__main__":
    main()
