"""Exercise the actual Bash mailbox reader with framed replies and EOF."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix", "requires Unix mailbox descriptors")
class PidReplyTests(unittest.TestCase):
    """Read injected reply bytes through the production exchange function."""

    def exchange(self, payload, calls=1):
        """Return a Bash result for complete, malformed or truncated frames.

        Parameters
        ----------
        payload : bytes
            Reply stream followed by EOF, simulating the manager writer.
        calls : int
            Number of GETs on the same kept mailbox.

        Returns
        -------
        subprocess.CompletedProcess
            Status and decoded replies from the production shell function.
        """
        library = Path(__file__).resolve().parents[2] / "contrib/preload/preload-manager.sh"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ready.json").write_text("{}\n")
            script = r'''
export PRELOAD_DIR=$2 PRELOAD_PID=1
source "$1" || exit 10
_PRELOAD_FD_DIR=$PRELOAD_DIR
_PRELOAD_MB_DIR=$PRELOAD_DIR
_PRELOAD_MB_PID=$BASHPID
_PRELOAD_MB_SLOT=0
_PRELOAD_MB_FD=0
exec {_PRELOAD_REQ_FD}>/dev/null
for ((i=0; i<$3; i++)); do
  _preload_unix_exchange GET git || exit 11
  printf '%s\n' "${_PRELOAD_REPLY#"$PRELOAD_DIR"}"
done
'''
            return subprocess.run([shutil.which("bash"), "--noprofile", "--norc",
                                   "-c", script, "test", str(library), directory,
                                   str(calls)], input=payload, capture_output=True,
                                  timeout=10)

    def test_adjacent_frames_and_hex_conversion(self):
        """Two GETs consume exactly eight bytes each without losing alignment."""
        result = self.exchange(b"0000007b000001ff", calls=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"/worker-123\n/worker-511\n")

    def test_error_preserves_diagnostic_and_next_frame(self):
        """Zero headers consume the error line before a later successful GET."""
        result = self.exchange(b"00000000ERROR not preloaded: missing\n0000007b", calls=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"ERROR not preloaded: missing\n/worker-123\n")

    def test_eof_and_invalid_frame_fail(self):
        """Manager death or malformed input cannot become a worker directory."""
        for payload in (b"", b"0000", b"zzzzzzzz", b"00000000ERROR cut off"):
            with self.subTest(payload=payload):
                result = self.exchange(payload)
                self.assertEqual(result.returncode, 11, result.stderr)
                self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
