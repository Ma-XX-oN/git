#!/usr/bin/env python3
"""Analyze exact native pipe metadata decision without assuming success/fallback."""
from __future__ import annotations
import argparse
from pathlib import Path
import re

FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
BLOCKED = FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT

def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("directory", type=Path)
  args = parser.parse_args()
  files = sorted(args.directory.glob("git809-detours-*.log"))
  assert len(files) >= 4, f"expected 4 read/write matched/unmatched cases: {files}"
  for path in files:
    name = path.name
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    assert any("op=HOOK_READY" in line for line in lines), name
    def find(op: str) -> list[str]:
      return [line for line in lines if f"op={op} " in line]
    markers = [line.strip() for line in lines if line.startswith("GIT809_PATH ")]
    assert markers, f"{name}: no actual MSYS control-flow markers captured"
    event_names = [event.split()[1] for event in markers]
    assert "QUERY_ENTER" in event_names, f"{name}: query path not entered: {markers}"
    for marker in markers:
      print(f"GIT809_MSYS_BRANCH file={name} {marker}", flush=True)
    queries = find("NtQueryAttributesFile_END")
    opens = find("NtCreateFile_END")
    closes = find("NtClose_END")
    for q in queries:
      status_match = re.search(r"status=0x([0-9a-fA-F]+)", q)
      attrs_match = re.search(r"opts=0x([0-9a-fA-F]+)", q)
      assert status_match and attrs_match, q
      status = int(status_match.group(1), 16)
      attrs = int(attrs_match.group(1), 16)
      succeeded = status < 0x80000000
      reasons = []
      if not succeeded:
        reasons.append("NT_QUERY_FAILED")
      elif attrs & FILE_ATTRIBUTE_DIRECTORY:
        reasons.append("DIRECTORY")
      elif attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        reasons.append("REPARSE_POINT")
      elif attrs & FILE_ATTRIBUTE_SYSTEM:
        reasons.append("SYSTEM_FILE")
      else:
        reasons.append("QUERY_ACCEPTANCE_CONDITIONS_MET")
      print(f"GIT809_QUERY_DECISION file={name} status=0x{status:08x} "
            f"attributes=0x{attrs:08x} reasons={','.join(reasons)}", flush=True)
    for event in opens:
      status = re.search(r"status=0x([0-9a-fA-F]+)", event)
      assert status, event
      print(f"GIT809_CREATE_RESULT file={name} native_status=0x{int(status.group(1),16):08x}",flush=True)
    if any("QUERY_ACCEPTANCE_CONDITIONS_MET" in reason for reason in [
        "QUERY_ACCEPTANCE_CONDITIONS_MET" if (
          (int(re.search(r"status=0x([0-9a-fA-F]+)", q).group(1), 16) < 0x80000000)
          and not (int(re.search(r"opts=0x([0-9a-fA-F]+)", q).group(1), 16) & BLOCKED)
        ) else "" for q in queries]):
      assert "QUERY_ACCEPTED" in event_names, f"{name}: query accepted by NT but not by MSYS"
      assert "BYPASS_GOTO" in event_names, f"{name}: accepted query missed bypass goto"
      assert "FILE_NOT_SYMLINK" in event_names, f"{name}: bypass target not executed"
    print(f"GIT809_CONTROL_FLOW file={name} markers={event_names!r} "
          f"metadata_fallthrough={'METADATA_OPEN_REACHED' in event_names} "
          f"fs_update_skipped={'FS_UPDATE_SKIPPED' in event_names}", flush=True)
    print(f"GIT809_CASE_SUMMARY file={name} query_count={len(queries)} "
          f"create_count={len(opens)} close_count={len(closes)} "
          f"query_invoked={int(bool(queries))} "
          f"metadata_open_after_query={int(bool(queries and opens))}",flush=True)
  assert len(files) == 4, "unexpected case count; do not silently merge unrelated traces"

if __name__ == "__main__":
  main()
