#!/usr/bin/env python3
"""Inspect the real tracerpt schema before attempting ETW IRP correlation."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path

def parse_pid(value):
  value = value.strip()
  try:
    return int(value, 0) if value.startswith(("0x", "0X")) else int(value)
  except ValueError:
    return None

def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--csv", required=True, type=Path)
  ap.add_argument("--manifest", required=True, type=Path)
  ap.add_argument("--output", required=True, type=Path)
  a = ap.parse_args()
  m = json.loads(a.manifest.read_text(encoding="utf-8"))
  assert len(m["operations"]) == 24 and m["complete"], "unexpected historical fixture"
  pids = {entry["pid"] for entry in m["operations"]}
  counts, matched = Counter(), Counter()
  examples = []
  per_kind = {}
  with a.csv.open(encoding="utf-8-sig", errors="replace", newline="") as f:
    reader = csv.reader(f)
    header = [x.strip() for x in next(reader)]
    if len(set(header)) != len(header):
      raise ValueError("ambiguous duplicate CSV header columns")
    ix = {name: i for i, name in enumerate(header)}
    for field in ("Event Name", "Type", "PID", "TID", "Clock-Time"):
      if field not in ix:
        raise ValueError(f"missing ETW CSV field: {field!r}")
    rows = 0
    for row in reader:
      rows += 1
      if len(row) < len(header):
        row += [""] * (len(header) - len(row))
      typ = row[ix["Type"]].strip()
      event = row[ix["Event Name"]].strip()
      counts[(event, typ)] += 1
      pid = parse_pid(row[ix["PID"]])
      if pid not in pids:
        continue
      matched[(event, typ)] += 1
      if event == "FileIo" and typ in ("Create", "OperationEnd", "QueryInfo",
                                      "Read", "Close", "Cleanup"):
        if len(per_kind.setdefault(typ, [])) < 4:
          values = [{"position": i, "field": header[i], "value": col[:200]}
                    for i, col in enumerate(row[:len(header)]) if col.strip()]
          per_kind[typ].append({"pid": pid, "fields": values})
      if len(examples) < 20 and event in ("FileIo", "DiskIo", "Process"):
        examples.append({"event": event, "type": typ, "pid": pid})
  report = {
    "source_revision": m["revision"],
    "git_binary_sha256": m["git_sha256"],
    "original_etl_sha256": m.get("trace_file", {}).get("sha256"),
    "process_count": len(m["operations"]),
    "git_pids": sorted(pids),
    "csv_header": [{"index": i, "name": field} for i, field in enumerate(header)],
    "events_total": rows,
    "event_counts": [{"event": k[0], "type": k[1], "count": v}
                     for k, v in counts.most_common(80)],
    "matched_git_pid_counts": [{"event": k[0], "type": k[1], "count": v}
                               for k, v in matched.most_common(80)],
    "fileio_exact_git_pid_samples": per_kind,
    "examples": examples,
  }
  a.output.parent.mkdir(parents=True, exist_ok=True)
  a.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
  print("ETW_ROWS", rows)
  print("ETW_FIELDS", json.dumps(report["csv_header"]))
  print("ETW_GIT_PID_COUNTS", json.dumps(report["matched_git_pid_counts"][:24]))
  for kind, sample in sorted(per_kind.items()):
    print("ETW_SAMPLE", kind, json.dumps(sample[:2])[:7000])
  assert rows > 100000, "historical trace unexpectedly short"
  assert matched[("FileIo", "Create")] > 0, "no matched process file Create events"
  assert matched[("FileIo", "OperationEnd")] > 0, "no matched file OperationEnd events"
  print("ETW_SCHEMA_CAPTURED", m["revision"])

if __name__ == "__main__":
  main()
