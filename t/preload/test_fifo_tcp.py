"""Check the communication benchmark's stream framing and failure handling."""

import importlib.util
import os
from pathlib import Path
import socket
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "compare_fifo_tcp", Path(__file__).parents[1] / "perf/compare_fifo_tcp.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


@unittest.skipUnless(os.name == "posix", "socket descriptors require POSIX os.read")
class ProtocolTest(unittest.TestCase):
    """Exercise real stream descriptors with fragmented and invalid requests."""

    def test_fragmented_and_coalesced_requests(self):
        """Preserve request boundaries when stream reads split or merge writes."""
        client, server = socket.socketpair()
        errors = []

        def run():
            """Capture protocol exceptions from the server thread."""
            try:
                BENCH.serve(server.fileno(), server.fileno(), b"000000ff\n", 3)
            except Exception as error:
                errors.append(error)

        with client, server:
            client.settimeout(5)
            thread = threading.Thread(target=run)
            thread.start()
            client.sendall(b"GE")
            client.sendall(b"T\nGET\nGET\n")
            result = b""
            while len(result) < 27:
                result += client.recv(27 - len(result))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, b"000000ff\n" * 3)

    def test_invalid_request_and_early_eof(self):
        """Reject malformed requests and incomplete exchanges explicitly."""
        for request, error in ((b"BAD\n", ValueError), (b"GE", EOFError)):
            with self.subTest(request=request):
                client, server = socket.socketpair()
                with client, server:
                    client.sendall(request)
                    client.shutdown(socket.SHUT_WR)
                    with self.assertRaises(error):
                        BENCH.serve(server.fileno(), server.fileno(), b"reply", 1)


if __name__ == "__main__":
    unittest.main()
