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
  p.add_argument("--objdump", required=True, type=Path)
  a = p.parse_args()
  assert a.debug_file.is_file() and a.debug_file.stat().st_size > 1000
  assert a.dll.is_file() and a.addr2line.is_file() and a.objdump.is_file()
  header = subprocess.run([str(a.objdump), "-p", str(a.dll)], capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout
  preferred = re.search(r"(?m)^ImageBase\s+(?:0x)?([0-9a-fA-F]+)\s*$", header)
  image_size = re.search(r"(?m)^SizeOfImage\s+(?:0x)?([0-9a-fA-F]+)\s*$", header)
  assert preferred and image_size, "DLL PE image base/size not found"
  preferred_base = int(preferred.group(1), 16)
  mapped_size = int(image_size.group(1), 16)
  assert preferred_base > 0 and mapped_size > 0
  print(f"GIT809_IMAGE preferred=0x{preferred_base:x} size=0x{mapped_size:x}", flush=True)
  traces = sorted(a.trace_dir.glob("git809-detours-*.log"))
  assert len(traces) >= 2, f"Expected read/write Detours traces, got {traces}"
  observations = 0
  resolved = 0
  categories = set()
  for f in traces:
    content = f.read_text(encoding="utf-8", errors="strict").splitlines()
    assert any("op=HOOK_READY" in line for line in content)
    for line in content:
      if "op=NtCreateFile_END" not in line and "op=NtClose_END" not in line:
        continue
      status = re.search(r"status=0x([0-9a-fA-F]+)", line)
      assert status, f"Missing native result status: {line}"
      operation = "NtClose" if "op=NtClose_END" in line else "NtCreateFile"
      result = int(status.group(1), 16)
      kind = ("metadata-open" if operation == "NtCreateFile" and result == 0
              else "transport-open-busy" if operation == "NtCreateFile" and result == 0xC00000AC
              else "metadata-close" if operation == "NtClose" and result == 0
              else "other")
      if kind != "other":
        categories.add((f.name.split("-")[2], kind))
      base = re.search(r"imagebase=(0x[0-9a-fA-F]+|[0-9a-fA-F]+)", line)
      stack = re.search(r"stack=([0-9a-fA-Fx,]+)", line)
      if not base or not stack:
        raise AssertionError(f"Missing module base or stack: {line}")
      imagebase = int(base.group(1), 16)
      assert imagebase > 0, "MSYS2 runtime module base missing"
      addresses = [int(x, 16) for x in stack.group(1).rstrip(",").split(",")]
      # PE has a preferred load address; Detours records the actual mapped
      # image base. Convert each runtime address to a preferred-image VMA.
      runtime = [x for x in addresses if imagebase <= x < imagebase + mapped_size]
      for addr in runtime:
        # GNU addr2line expects PE preferred-VMA addresses; adjust from
        # the actual mapped image base to the debug file's ImageBase.
        canonical = preferred_base + (addr - imagebase)
        assert preferred_base <= canonical < preferred_base + mapped_size
        result = subprocess.run([str(a.addr2line), "-f", "-C", "-e", str(a.debug_file),
                                 f"0x{canonical:x}"], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        fn, _, location = result.stdout.partition("\n")
        print(f"GIT809_SYMBOL file={f.name} kind={kind} op={operation} "
              f"status=0x{result:08x} base=0x{imagebase:x} "
              f"address=0x{addr:x} preferred=0x{canonical:x} "
              f"function={fn} source={location.strip()}", flush=True)
        observations += 1
        if fn != "??" and not location.strip().startswith("??:"):
          resolved += 1
  for direction in ("read","write"):
    for kind in ("metadata-open","metadata-close","transport-open-busy"):
      assert (direction,kind) in categories, f"Missing {direction} {kind} event"
  assert observations > 0, "No MSYS2 runtime frames captured"
  assert resolved > 0, "No MSYS2 runtime frames resolve to source"
  print(f"GIT809_SYMBOL_SUMMARY frames={observations} with_source={resolved}")
  return 0

if __name__ == "__main__":
  sys.exit(main())
