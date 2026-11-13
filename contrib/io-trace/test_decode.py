#!/usr/bin/env python3
"""Direct, deterministic regression for the version-1 binary trace format."""
import tempfile
import unittest
from pathlib import Path

import decode


class BinaryTraceDecoderTest(unittest.TestCase):
  def trace_bytes(self, include_end=True, exit_kind=3):
    pid = 100
    tid = 200
    header = decode.HEADER.pack(
      b"GITIO001", 1, 0x01020304, decode.EVENT.size, 0,
      1000000000, pid)

    def event(t, kind, value=0, argument=0, handle=0,
              status=0, path_len=0):
      return decode.EVENT.pack(
        t, value & ((1 << 64) - 1), argument, handle,
        tid, pid, status, kind, 0, path_len, 0)

    filename = b"fd-zero.txt"
    records = [
      event(100, 0),
      event(110, 2, path_len=len(filename)),
      event(120, exit_kind, value=0, handle=0),
      filename,
      event(130, 4, handle=0, argument=7),
      event(140, 5, value=7, handle=0),
      event(150, 1) if include_end else b"",
    ]
    return header + b"".join(records)

  def decode(self, content):
    with tempfile.TemporaryDirectory() as d:
      path = Path(d) / "trace.bin"
      path.write_bytes(content)
      return decode.parse(path)

  def test_zero_descriptor_path_and_counts(self):
    trace = self.decode(self.trace_bytes())
    operations = trace["operations"]
    self.assertEqual(len(operations), 2)
    self.assertEqual([o["operation"] for o in operations],
                     ["OPEN", "READ"])
    self.assertEqual([o["path"] for o in operations],
                     ["fd-zero.txt", "fd-zero.txt"])
    self.assertEqual(operations[1]["handle"], 0)
    report = decode.summary(trace)
    self.assertEqual(report["by_operation"]["OPEN"]["count"], 1)
    self.assertEqual(report["by_operation"]["READ"]["count"], 1)
    self.assertAlmostEqual(report["unattributed_ms"], 30 / 1000000)

  def test_missing_end_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "missing process START/END"):
      self.decode(self.trace_bytes(include_end=False))

  def test_mismatched_exit_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "unmatched"):
      self.decode(self.trace_bytes(exit_kind=5))

  def test_truncated_record_is_rejected(self):
    with self.assertRaises(ValueError):
      self.decode(self.trace_bytes()[:-1])


if __name__ == "__main__":
  unittest.main()
