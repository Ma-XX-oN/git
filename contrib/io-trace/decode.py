#!/usr/bin/env python3
"""Decode investigation-only git binary I/O logs (version 1)."""
import argparse
import collections
import json
import statistics
import struct
from pathlib import Path

HEADER = struct.Struct("<8sIIIIQQ")
EVENT = struct.Struct("<QQQQQIiHHII4x")
NAMES = (
  "PROCESS", "OPEN", "READ", "WRITE", "CLOSE", "STAT", "LSTAT",
  "FSTAT", "OPENDIR", "READDIR", "CLOSEDIR", "FOPEN", "FREAD",
  "FWRITE", "FCLOSE"
)
PATH_TRUNCATED = 1


def signed(value):
  return value - (1 << 64) if value & (1 << 63) else value


def parse(path):
  data = Path(path).read_bytes()
  if len(data) < HEADER.size:
    raise ValueError("truncated header")
  magic, version, endian, recsize, reserved, frequency, pid = HEADER.unpack_from(data)
  if (magic, version, endian, recsize, reserved) != (
      b"GITIO001", 1, 0x01020304, EVENT.size, 0):
    raise ValueError("incompatible header or byte order")
  if not frequency:
    raise ValueError("zero clock frequency")

  offset = HEADER.size
  operations = []
  process_events = {}
  fd_paths = {}
  stream_paths = {}
  dir_paths = {}
  while offset < len(data):
    if len(data) - offset < EVENT.size:
      raise ValueError("truncated event")
    a = EVENT.unpack_from(data, offset)
    offset += EVENT.size
    (begin, _, argument, handle, tid, event_pid, _, kind, flags,
     pathlen, _) = a
    if event_pid != pid or kind >= len(NAMES) * 2:
      raise ValueError("invalid event type or PID")
    if kind in (0, 1):
      if flags or pathlen:
        raise ValueError("invalid process record")
      if kind in process_events:
        raise ValueError("duplicate process event")
      process_events[kind] = begin
      continue
    if kind & 1:
      raise ValueError("EXIT without ENTRY")
    if len(data) - offset < EVENT.size:
      raise ValueError("missing EXIT record")
    b = EVENT.unpack_from(data, offset)
    offset += EVENT.size
    (end, value, _, exit_handle, exit_tid, exit_pid, status,
     exit_kind, exit_flags, returned_len, _) = b
    if (exit_kind != kind + 1 or exit_pid != pid or exit_tid != tid
        or end < begin or not begin or not end
        or len(data) - offset < pathlen + returned_len):
      raise ValueError("unmatched, invalid or truncated event pair")
    raw_path = data[offset:offset + pathlen + returned_len]
    offset += len(raw_path)
    filename = raw_path.decode("utf-8", "surrogateescape")
    if flags & PATH_TRUNCATED or exit_flags & PATH_TRUNCATED:
      raise ValueError("truncated filename: trace is not fully attributable")
    op = NAMES[kind // 2]
    if op in ("OPEN", "FOPEN", "OPENDIR") and not status and exit_handle:
      target = (fd_paths if op == "OPEN" else
                stream_paths if op == "FOPEN" else dir_paths)
      target[exit_handle] = filename
    if op in ("READ", "WRITE", "CLOSE", "FSTAT"):
      filename = fd_paths.get(handle, filename)
    elif op in ("FREAD", "FWRITE", "FCLOSE"):
      filename = stream_paths.get(handle, filename)
    elif op in ("READDIR", "CLOSEDIR"):
      directory = dir_paths.get(handle, "")
      if op == "READDIR" and filename:
        filename = directory.rstrip("/\\") + "/" + filename
      else:
        filename = directory
    if op == "CLOSE" and not status:
      fd_paths.pop(handle, None)
    elif op == "FCLOSE" and not status:
      stream_paths.pop(handle, None)
    elif op == "CLOSEDIR" and not status:
      dir_paths.pop(handle, None)
    operations.append({
      "operation": op, "pid": pid, "tid": tid, "path": filename,
      "handle": handle, "requested": argument, "result": signed(value),
      "status": status, "start_ticks": begin, "end_ticks": end,
      "duration_ms": (end - begin) * 1000.0 / frequency,
    })

  if set(process_events) != {0, 1}:
    raise ValueError("missing process START/END: incomplete trace")
  start, end = process_events[0], process_events[1]
  if end < start or not start:
    raise ValueError("invalid process interval")
  if any(o["start_ticks"] < start or o["end_ticks"] > end for o in operations):
    raise ValueError("I/O operation outside process interval")
  return {
    "file": str(path), "pid": pid, "clock_frequency": frequency,
    "process_ms": (end - start) * 1000.0 / frequency,
    "operations": operations
  }


def stats(samples):
  numbers = sorted(samples)
  if not numbers:
    return {"count": 0, "total_ms_inclusive": 0}
  return {
    "count": len(numbers),
    "total_ms_inclusive": round(sum(numbers), 6),
    "median_ms": round(statistics.median(numbers), 6),
    "p95_ms": round(numbers[(95 * (len(numbers) - 1)) // 100], 6),
    "max_ms": round(numbers[-1], 6)
  }


def summary(trace):
  by_operation = collections.defaultdict(list)
  by_path = collections.defaultdict(list)
  failures = collections.Counter()
  spans = []
  for item in trace["operations"]:
    by_operation[item["operation"]].append(item["duration_ms"])
    by_path[item["path"]].append(item["duration_ms"])
    if item["status"]:
      failures[(item["operation"], item["status"])] += 1
    spans.append((item["start_ticks"], item["end_ticks"]))
  spans.sort()
  active = 0
  if spans:
    left, right = spans[0]
    for start, end in spans[1:]:
      if start > right:
        active += right - left
        left, right = start, end
      else:
        right = max(right, end)
    active += right - left
  occupied_ms = active * 1000.0 / trace["clock_frequency"]
  return {
    "file": trace["file"], "pid": trace["pid"],
    "process_ms": round(trace["process_ms"], 6),
    "io_union_ms": round(occupied_ms, 6),
    "unattributed_ms": round(trace["process_ms"] - occupied_ms, 6),
    "by_operation": {name: stats(values) for name, values in sorted(by_operation.items())},
    "by_filename": [
      {"path": name, **stats(values)}
      for name, values in sorted(by_path.items(),
                                 key=lambda item: sum(item[1]), reverse=True)[:50]
      if name
    ],
    "failures": [
      {"operation": op, "status": status, "count": count}
      for (op, status), count in failures.most_common()
    ]
  }


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("logs", nargs="+", type=Path)
  parser.add_argument("--events", action="store_true",
                      help="Include all individual operation events")
  args = parser.parse_args()
  reports = []
  for path in args.logs:
    trace = parse(path)
    result = summary(trace)
    if args.events:
      result["events"] = trace["operations"]
    reports.append(result)
  print(json.dumps(reports, indent=2, ensure_ascii=True))


if __name__ == "__main__":
  main()
