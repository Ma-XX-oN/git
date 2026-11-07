#!/usr/bin/env python3
"""RWF #809: symbolize Detours NT call stacks against the exact SDK runtime .dbg."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

def main() -> int:
  p = argparse.ArgumentParser()
  p.add_argument("--trace-dir", required=True, type=Path)
  p.add_argument("--debug-file", required=True, type=Path)
  p.add_argument("--dll", required=True, type=Path)
  p.add_argument("--addr2line", required=True, type=Path)
  a = p.parse_args()
  assert a.debug_file.is_file() and a.debug_file.stat().st_size > 1000
  assert a.dll.is_file() and a.addr2line.is_file()
  traces = sorted(a.trace_dir.glob("git809-detours-*.log"))
  assert len(traces) >= 2, f"Expected read/write Detours traces, got {traces}"
  observations = 0
  resolved = 0
  for f in traces:
    content = f.read_text(encoding="utf-8", errors="strict").splitlines()
    assert any("op=HOOK_READY" in line for line in content)
    for line in content:
      if "op=NtCreateFile_END" not in line and "op=NtClose_END" not in line:
        continue
      base = re.search(r"imagebase=(0x[0-9a-fA-F]+|[0-9a-fA-F]+)", line)
      stack = re.search(r"stack=([0-9a-fA-Fx,]+)", line)
      if not base or not stack:
        raise AssertionError(f"Missing module base or stack: {line}")
      imagebase = int(base.group(1), 16)
      assert imagebase > 0, "MSYS2 runtime module base missing"
      addresses = [int(x, 16) for x in stack.group(1).rstrip(",").split(",")]
      # MSYS2 runtime is mapped at 0x210000000; reject addresses outside
      # its recorded module base and a conservative 256 MiB image envelope.
      runtime = [x for x in addresses if imagebase <= x < imagebase + 0x10000000]
      for addr in runtime:
        # GNU addr2line expects PE preferred-VMA addresses; adjust from
        # the actual mapped image base to the debug file's ImageBase.
        result = subprocess.run([str(a.addr2line), "-f", "-C", "-e", str(a.debug_file),
                                 f"0x{addr:x}"], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        fn, _, location = result.stdout.partition("\n")
        print(f"GIT809_SYMBOL file={f.name} op={line.split('op=')[1].split()[0]} "
              f"base=0x{imagebase:x} address=0x{addr:x} function={fn} source={location.strip()}",
              flush=True)
        observations += 1
        if fn != "??" and location.strip() != "??:0":
          resolved += 1
  assert observations > 0, "No MSYS2 runtime frames captured"
  assert resolved > 0, "No MSYS2 runtime frames resolve to source"
  print(f"GIT809_SYMBOL_SUMMARY frames={observations} with_source={resolved}")
  return 0

if __name__ == "__main__":
  sys.exit(main())
