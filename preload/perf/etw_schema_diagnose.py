#!/usr/bin/env python3
"""Inspect tracerpt CSV schema and representative ETW FileIo/Process rows.

No operation timings are accepted before verifying the actual fields.
"""
import collections
import csv
import json
from pathlib import Path
import sys


def examine(path, manifest_file, output):
  manifest = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
  assert manifest["complete"] and len(manifest["operations"]) == 24
  git_pids = {int(x["pid"]) for x in manifest["operations"]}
  assert len(git_pids) >= 16
  total = 0
  counts = collections.Counter()
  target = collections.Counter()
  samples = {}
  per_type = collections.defaultdict(list)
  with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
    reader = csv.reader(handle)
    header = [h.strip() for h in next(reader)]
    if len(header) < 10:
      raise ValueError(f"unexpected short header {header!r}")
    columns = {name: i for i, name in enumerate(header)}
    for name in ("Event Name", "Type", "PID", "TID", "Clock-Time"):
      if name not in columns:
        raise ValueError(f"missing {name}: {header!r}")
    for row in reader:
      total += 1
      if len(row) < len(header) // 2:
        continue
      kind = row[columns["Event Name"]].strip()
      typ = row[columns["Type"]].strip()
      key = f"{kind}/{typ}"
      counts[key] += 1
      try:
        raw_pid = row[columns["PID"]].strip()
        pid = int(raw_pid, 0) if raw_pid.lower().startswith("0x") else int(raw_pid)
      except (ValueError, IndexError):
        pid = None
      if pid in git_pids:
        target[key] += 1
      if kind not in ("FileIo", "DiskIo", "Process", "Thread"):
        continue
      if len(per_type[key]) < 5:
        per_type[key].append({
          "git_pid": pid in git_pids,
          "pid": pid,
          "fields": row[:min(len(row), 36)],
          "mapped": {key_: row[idx] if idx < len(row) else None
                     for key_, idx in columns.items()},
        })
      if key not in samples:
        samples[key] = row[:36]
  if total < 200 or not any(k.startswith("FileIo/") for k in counts):
    raise AssertionError(f"ETW schema lacked file events; rows={total}")
  report = {
    "source_revision": manifest["revision"],
    "source_git_sha256": manifest["git_sha256"],
    "etl_sha256": manifest["trace_file"]["sha256"],
    "target_pids": sorted(git_pids),
    "total_rows": total,
    "header": header,
    "all_event_counts": counts.most_common(),
    "exact_git_pid_event_counts": target.most_common(),
    "examples_by_type": dict(per_type),
  }
  Path(output).write_text(json.dumps(report, indent=2), encoding="utf-8")
  print("ETW_SCHEMA_TOTAL", total)
  print("ETW_HEADER", json.dumps(header))
  print("ETW_TARGET_PID_MATCH", sum(target.values()))
  print("ETW_TYPES", json.dumps(counts.most_common(30)))
  for key in ("FileIo/Create", "FileIo/OperationEnd", "FileIo/QueryInfo"):
    print("ETW_SAMPLE", key, json.dumps(per_type.get(key, [])[:2])[:4000])
  return report


if __name__ == "__main__":
  if len(sys.argv) != 4:
    raise SystemExit("usage: etw_schema_diagnose.py csv manifest output.json")
  examine(*sys.argv[1:])
