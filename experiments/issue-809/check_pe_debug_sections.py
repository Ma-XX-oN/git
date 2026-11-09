#!/usr/bin/env python3
"""Verify that a PE/COFF executable retains source-level DWARF sections."""
import argparse
from pathlib import Path
import struct


def read_u16(data, offset):
  if offset < 0 or offset + 2 > len(data):
    raise ValueError("truncated PE/COFF u16 field")
  return struct.unpack_from("<H", data, offset)[0]


def read_u32(data, offset):
  if offset < 0 or offset + 4 > len(data):
    raise ValueError("truncated PE/COFF u32 field")
  return struct.unpack_from("<I", data, offset)[0]


def check_sections(path):
  data = Path(path).read_bytes()
  if data[:2] != b"MZ":
    raise ValueError("missing MZ executable signature")
  pe = read_u32(data, 0x3c)
  if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
    raise ValueError("missing PE signature")
  count = read_u16(data, pe + 6)
  symptr = read_u32(data, pe + 12)
  symbols = read_u32(data, pe + 16)
  optional = read_u16(data, pe + 20)
  start = pe + 24 + optional
  if not count or start + count * 40 > len(data):
    raise ValueError("invalid section table")
  strings = symptr + 18 * symbols
  names = set()
  for n in range(count):
    raw = data[start + 40 * n:start + 40 * n + 8].split(b"\0", 1)[0]
    if raw.startswith(b"/") and raw[1:].isdigit():
      # GNU PE/COFF stores long section names in the COFF string table.
      if not symptr or strings + 4 > len(data):
        raise ValueError("missing COFF string table")
      size = read_u32(data, strings)
      pos = strings + int(raw[1:])
      if size < 4 or strings + size > len(data) or pos >= strings + size:
        raise ValueError("invalid COFF section name offset")
      end = data.find(b"\0", pos, strings + size)
      if end < 0:
        raise ValueError("unterminated COFF section name")
      raw = data[pos:end]
    names.add(raw.decode("ascii"))
  required = {".debug_info", ".debug_line"}
  missing = required - names
  if missing:
    raise ValueError("missing DWARF sections: " + ", ".join(sorted(missing)))
  print("GIT809_DWARF_SECTIONS_OK", path, *sorted(required), flush=True)


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("exe")
  args = parser.parse_args()
  check_sections(args.exe)
