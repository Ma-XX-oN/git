#!/usr/bin/env python3
"""Resolve matching Detours NT API stacks against the *same* runtime DLL and .dbg.

Requires objdump and addr2line from the Git-for-Windows SDK. Fails closed if no
runtime addresses or source locations can be resolved.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

def pe_property(text: str, name: str) -> int:
  found = re.search(r"^\\s*" + re.escape(name) + r"\\s+([0-9a-fA-F]+)\\b",
                    text, flags=re.MULTILINE)
  if not found:
    raise RuntimeError(f"PE header missing {name}")
  return int(found.group(1), 16)

def main() -> None:
  p = argparse.ArgumentParser()
  p.add_argument("--dll", type=Path, required=True)
  p.add_argument("--dbg", type=Path, required=True)
  p.add_argument("--traces", type=Path, nargs="+", required=True)
  p.add_argument("--output", type=Path, required=True)
  args = p.parse_args()

  for file in (args.dll, args.dbg, *args.traces):
    if not file.is_file() or not file.stat().st_size:
      raise RuntimeError(f"Missing or empty exact-build evidence: {file}")

  header = subprocess.run(["objdump", "-p", str(args.dll)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=True).stdout
  preferred = pe_property(header, "ImageBase")
  size = pe_property(header, "SizeOfImage")
  report = [f"GIT809_SYMBOL preferred_base=0x{preferred:x} size=0x{size:x}"]
  resolved = 0
  events = 0
  for trace in args.traces:
    for row in trace.read_text(encoding="utf-8", errors="replace").splitlines():
      if not row.startswith("GIT809_DETOURS ") or "op=" not in row:
        continue
      base = re.search(r"\\bruntime_base=([0-9A-Fa-f]+)", row)
      event = re.search(r"\\bop=([A-Za-z_]+)", row)
      stack = re.search(r"\\bstack=([0-9A-Fa-fx,]+)", row)
      if not base or not event or not stack:
        raise RuntimeError(f"Incomplete runtime base / stack in {trace}: {row[:180]}")
      loaded = int(base.group(1), 16)
      if loaded == 0:
        raise RuntimeError("Instrumented process did not load the expected MSYS2 DLL")
      addresses = [int(a, 16) for a in stack.group(1).split(",") if a]
      for address in addresses:
        if loaded <= address < loaded + size:
          events += 1
          adjusted = address - loaded + preferred
          symbol = subprocess.run(
            ["addr2line", "-f", "-C", "-e", str(args.dbg), f"0x{adjusted:x}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=True).stdout.strip().splitlines()
          if len(symbol) != 2:
            raise RuntimeError(f"Malformed addr2line output: {symbol}")
          frame = (f"GIT809_SYMBOL trace={trace.name} op={event.group(1)} "
                   f"absolute=0x{address:x} adjusted=0x{adjusted:x} "
                   f"function={symbol[0]} location={symbol[1]}")
          report.append(frame)
          if symbol[1] not in ("??:0", "??:?") and not symbol[1].startswith("??:"):
            resolved += 1
  report.append(f"GIT809_SYMBOL_SUMMARY runtime_frames={events} source_locations={resolved}")
  args.output.write_text("\\n".join(report) + "\\n", encoding="utf-8")
  print("\\n".join(report), flush=True)
  if events == 0 or resolved == 0:
    raise RuntimeError("No exact-DLL runtime frames resolved to source locations")

if __name__ == "__main__":
  main()
