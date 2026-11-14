#!/usr/bin/env python3
"""Direct regression for the 22-field ETW OperationEnd parse boundary."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from etw_correlate import correlate, FILETIME_UNIX_OFFSET


class EtwPairRegression(unittest.TestCase):
  def fixture(self, root, end_width=22):
    base = 134390774028089693
    pid = 1808
    pids = list(range(pid, pid + 24))
    operations = [
      {
        "pid": p,
        "command": ["status", "--porcelain"] if p == pid else
                   ["rev-parse", "HEAD"],
        "start_utc_ns": (base - FILETIME_UNIX_OFFSET) * 100 - 1000000000,
        "end_utc_ns": (base + 1500000 - FILETIME_UNIX_OFFSET) * 100 +
                       1000000000,
      } for p in pids
    ]
    manifest = {
      "complete": True,
      "revision": "fixture-not-live",
      "git_sha256": "00000000",
      "trace_file": {"sha256": "00000000"},
      "operations": operations,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    headers = [f"field_{i}" for i in range(26)]
    headers[0] = "Event Name"
    headers[1] = "Type"
    headers[9] = "PID"
    headers[16] = "Clock-Time"
    with (root / "events.csv").open("w", newline="", encoding="utf-8") as handle:
      writer = csv.writer(handle)
      writer.writerow(headers)
      for i in range(1002):
        kind = "Create" if i % 2 == 0 else "QueryInfo"
        irp = f"0x{i + 1:016X}"
        start = [""] * 26
        start[0], start[1] = "FileIo", kind
        start[9], start[16], start[19] = hex(pid), str(base + i * 1000), irp
        start[25] = "C:\\fixture\\data.txt" if kind == "Create" else ""
        writer.writerow(start)
        end = [""] * end_width
        end[0], end[1] = "FileIo", "OperationEnd"
        end[9], end[16], end[19] = hex(pid), str(base + i * 1000 + 659), irp
        if end_width >= 22:
          end[21] = "0"
        writer.writerow(end)
    return root / "events.csv", root / "manifest.json"

  def test_22_field_operation_end_matches(self):
    with tempfile.TemporaryDirectory() as d:
      root = Path(d)
      csv_path, manifest = self.fixture(root)
      report = correlate(csv_path, manifest, root / "result.json")
      self.assertEqual(report["matched_git_operations"], 1002)
      self.assertEqual(report["duration_by_operation"]["Create"]["count"], 501)
      self.assertEqual(report["duration_by_operation"]["QueryInfo"]["count"], 501)
      self.assertEqual(report["duration_by_operation"]["Create"]["median_us"], 65.9)
      self.assertEqual(report["unmatched_operation_end_records"], {})

  def test_short_operation_end_cannot_claim_complete(self):
    with tempfile.TemporaryDirectory() as d:
      root = Path(d)
      csv_path, manifest = self.fixture(root, end_width=21)
      with self.assertRaisesRegex(AssertionError, "Only 0 matched"):
        correlate(csv_path, manifest, root / "result.json")


if __name__ == "__main__":
  unittest.main()
