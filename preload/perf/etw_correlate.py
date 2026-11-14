#!/usr/bin/env python3
"""Correlate existing Windows ETW FileIo starts with OperationEnd by IRP.

Uses observed tracerpt CSV field shapes. The critical old error was rejecting
OperationEnd records because they have 22 fields, not >22. Attribution is
strictly to the 24 recorded Git PIDs, not unrelated Windows activity.
"""
import collections
import csv
import json
from pathlib import Path
import statistics
import sys

OPERATIONS = {
  "Create", "Read", "Write", "QueryInfo", "Close", "Cleanup",
  "DirEnum", "FSControl", "SetInfo", "Flush",
}
FILETIME_UNIX_OFFSET = 116444736000000000


def aggregate(values):
  values = sorted(values)
  if not values:
    return {"count": 0}
  return {
    "count": len(values),
    "median_us": round(statistics.median(values) / 10, 3),
    "p95_us": round(values[(95 * (len(values) - 1)) // 100] / 10, 3),
    "mean_us": round(statistics.mean(values) / 10, 3),
    "total_ms_inclusive": round(sum(values) / 10000, 3),
    "max_us": round(values[-1] / 10, 3),
  }


def correlate(csv_path, manifest_path, result_path):
  manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
  assert manifest["complete"] and len(manifest["operations"]) == 24
  by_pid = {int(x["pid"]): x for x in manifest["operations"]}
  assert len(by_pid) == 24, "PID reuse across the 24 calls is not supported"
  starts = {}
  totals = collections.Counter()
  direct = collections.Counter()
  unmatched = collections.Counter()
  ignored = collections.Counter()
  collisions = 0
  windows_mismatches = 0
  paired = collections.defaultdict(list)
  per_command = collections.defaultdict(list)
  path_durations = collections.defaultdict(list)
  status_codes = collections.Counter()
  exact_pid_samples = {}
  matched = 0
  header = None

  with open(csv_path, encoding="utf-8-sig",
            errors="replace", newline="") as stream:
    reader = csv.reader(stream)
    header = [h.strip() for h in next(reader)]
    col = {name: i for i, name in enumerate(header)}
    for name in ("Event Name", "Type", "PID", "Clock-Time"):
      if name not in col:
        raise ValueError(f"missing ETW field {name}: {header}")
    for row in reader:
      if len(row) <= max(col.values()):
        # tracerpt events have event-type-specific trailing payload columns.
        # Only the shared columns must exist.
        if len(row) <= max(col[name] for name in
                           ("Event Name", "Type", "PID", "Clock-Time")):
          ignored["short_common_header"] += 1
          continue
      kind = row[col["Event Name"]].strip()
      typ = row[col["Type"]].strip()
      totals[f"{kind}/{typ}"] += 1
      if kind != "FileIo":
        continue
      try:
        raw_pid = row[col["PID"]].strip()
        pid = int(raw_pid, 16) if raw_pid.lower().startswith("0x") else int(raw_pid)
        timestamp = int(row[col["Clock-Time"]].strip())
      except (ValueError, IndexError):
        ignored["bad_pid_or_time"] += 1
        continue
      if len(row) < 20:
        ignored["no_irp_column"] += 1
        continue
      irp = row[19].strip().upper()
      if not irp or irp in ("0", "0X0000000000000000"):
        ignored["null_irp"] += 1
        continue

      if typ in OPERATIONS:
        if irp in starts:
          # Refuse to misattribute ambiguous, concurrently reused IRPs.
          collisions += 1
          starts.pop(irp)
          ignored["ambiguous_irp"] += 1
          continue
        filename = (row[25].strip().strip('"') if typ == "Create" and
                    len(row) > 25 else "")
        starts[irp] = (pid, timestamp, typ, filename)
        if pid in by_pid:
          direct[f"FileIo/{typ}"] += 1
      elif typ == "OperationEnd":
        # This event has EXACTLY 22 fields in the captured tracerpt CSV.
        # Prior code required len(row) > 22, silently discarding every end.
        if len(row) != 22:
          ignored["unexpected_end_columns"] += 1
          continue
        start = starts.pop(irp, None)
        if start is None:
          unmatched["end_without_start"] += 1
          continue
        start_pid, began, opname, filename = start
        if start_pid not in by_pid:
          continue
        duration_ticks = timestamp - began
        if duration_ticks < 0 or duration_ticks > 100000000:
          ignored["invalid_duration"] += 1
          continue
        # OperationEnd payload: Irp at [19], ExtraInfo at [20],
        # NTSTATUS at [21], verified against the recorded 22-field event.
        try:
          code = int(row[21].strip(), 0)
        except ValueError:
          ignored["invalid_ntstatus"] += 1
          continue
        # The event timestamps are FILETIME ticks (100 ns since 1601).
        end_unix_ns = (timestamp - FILETIME_UNIX_OFFSET) * 100
        begin_unix_ns = (began - FILETIME_UNIX_OFFSET) * 100
        op = by_pid[start_pid]
        if not (op["start_utc_ns"] - 2000000000 <= begin_unix_ns and
                end_unix_ns <= op["end_utc_ns"] + 2000000000):
          windows_mismatches += 1
          continue
        matched += 1
        paired[opname].append(duration_ticks)
        command = " ".join(op["command"])
        per_command[command].append(duration_ticks)
        if filename:
          leaf = filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
          path_durations[leaf].append(duration_ticks)
        if code:
          status_codes[f"{opname}/0x{code:08X}"] += 1
        if start_pid not in exact_pid_samples:
          exact_pid_samples[start_pid] = {
            "operation": opname,
            "duration_us": round(duration_ticks / 10, 3),
            "pid": start_pid,
            "command": command,
            "irp": irp,
          }
        direct["FileIo/OperationEnd"] += 1

  if matched < 1000:
    raise AssertionError(
      f"Only {matched} matched native FileIo begin/end pairs; "
      f"unmatched={dict(unmatched)}, rejected={dict(ignored)}, "
      f"collisions={collisions}, time_window_mismatches={windows_mismatches}")
  if not paired.get("Create") or not paired.get("QueryInfo"):
    raise AssertionError("Missing independently required Create/QueryInfo evidence")

  report = {
    "schema": 1,
    "method": "tracerpt CSV, FileIo IRP start/end, 100ns FILETIME",
    "capture_revision": manifest["revision"],
    "git_binary_sha256": manifest["git_sha256"],
    "etl_sha256": manifest["trace_file"]["sha256"],
    "source_csv_header": header,
    "recorded_pids": sorted(by_pid),
    "matched_git_operations": matched,
    "unique_git_pids_with_matched_operations": len(exact_pid_samples),
    "incomplete_begin_records": sum(1 for pid, *_ in starts.values() if pid in by_pid),
    "unmatched_operation_end_records": dict(unmatched),
    "rejected": dict(ignored),
    "ambiguous_irp_collisions": collisions,
    "out_of_manifest_window": windows_mismatches,
    "all_events_by_type": dict(totals),
    "git_events_by_type": dict(direct),
    "duration_by_operation": {k: aggregate(v) for k, v in sorted(paired.items())},
    "duration_by_git_command": {k: aggregate(v) for k, v in sorted(per_command.items())},
    "expensive_created_filenames": [
      {"name": name, **aggregate(vals)}
      for name, vals in sorted(path_durations.items(),
                               key=lambda item: sum(item[1]), reverse=True)[:25]
    ],
    "nonzero_ntstatus": dict(status_codes.most_common(25)),
    "sample_one_per_pid": list(exact_pid_samples.values()),
    "limitation": (
      "ETW file event duration includes Windows filesystem completion latency "
      "and monitoring overhead. Inclusive durations overlap and do not prove "
      "filter-driver or physical-disk causation."
    ),
  }
  output = Path(result_path)
  output.parent.mkdir(parents=True, exist_ok=True)
  output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
  print("ETW_PAIRED_GIT_OPERATIONS", matched)
  print("ETW_PIDS_WITH_PAIRS", len(exact_pid_samples), "of", len(by_pid))
  print("ETW_NATIVE_LATENCY", json.dumps(report["duration_by_operation"]))
  print("ETW_COMMAND_LATENCY", json.dumps(report["duration_by_git_command"]))
  print("ETW_CORRELATION_DIAGNOSTIC", json.dumps({
    "unmatched": dict(unmatched), "ignored": dict(ignored),
    "collisions": collisions, "time_window_mismatches": windows_mismatches
  }))
  return report


if __name__ == "__main__":
  if len(sys.argv) != 4:
    raise SystemExit("usage: etw_correlate.py events.csv trace-manifest.json report.json")
  correlate(*sys.argv[1:])
