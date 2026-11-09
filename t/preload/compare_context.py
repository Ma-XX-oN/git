"""Compare Windows context semantics using probes linked against both versions.

Run with --before PROBE.exe --after PROBE.exe --output DIRECTORY. Keep the
compiler's native DLL directory on PATH. Each probe uses its real libgit code;
the comparison includes empty environments and values beyond 32,767 characters.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess


def block(entries):
    """Encode a complete UTF-8 environment with its double NUL terminator."""
    return ("\0".join(entries) + "\0\0").encode("utf-8")


def binary(entries):
    """Frame the production binary environment tag."""
    value = block(entries)
    return b"env\0" + struct.pack("<I", len(value)) + value


def main():
    """Apply identical snapshots and persist complete baseline/candidate results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    initial = ["=C:=C:\\prior", "=D:=D:\\prior", "OLD=removed",
               "KEEP=before", "SystemRoot=C:\\Windows"]
    cases = [
        ("replace-unicode-empty", initial, binary([
            "KEEP=after", "EMPTY=", "UNICODE=é-☃-😀", "EQUAL=a=b"])),
        ("ascii-case-last-wins", initial,
         binary(["MiXeD=first", "MIXED=second", "mixed=last"])),
        ("unicode-case-last-wins", initial, binary(["É=first", "é=last"])),
        ("rename-existing-key", initial, binary(["keep=before"])),
        ("unchanged-snapshot", initial, binary([
            "OLD=removed", "KEEP=before", "SystemRoot=C:\\Windows"])),
        ("empty-keeps-drive-directories", initial, binary([])),
        ("empty-no-drive-directories", ["OLD=removed"], binary([])),
        ("export-quotes-unicode", initial,
         ('env-export\0declare -x KEEP="after"\ndeclare -x EMPTY=""\n'
          'declare -x UNICODE="é-☃-😀"\n'
          "declare -x ESCAPED=$'line1\\nline2\\tquote\\\''\n\0").encode("utf-8")),
        ("export-case-last-wins", initial,
         b'env-export\0declare -x LOWER="first"\ndeclare -x lower="last"\n\0'),
        ("export-no-value", initial, b'env-export\0declare -x UNASSIGNED\n\0'),
        ("invalid-export", initial,
         b'env-export\0declare -x BAD="unterminated\n\0'),
        ("many-variables", initial,
         binary([f"KEY_{i:03d}=value-{i}" for i in range(300)])),
    ]
    for size in (32766, 32767, 32768, 40000):
        cases.append((f"value-size-{size}", initial, binary(["LONG=" + "x" * size])))
    report = {
        "probes": {name: {"path": str(exe.resolve()),
                          "sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
                   for name, exe in (("before", args.before), ("after", args.after))},
        "cases": [],
    }
    for name, base, frame in cases:
        initial_file = args.output / f"{name}-initial.bin"
        frame_file = args.output / f"{name}-frame.bin"
        initial_file.write_bytes(block(base))
        frame_file.write_bytes(frame)
        observed = []
        for exe in (args.before, args.after):
            process = subprocess.run(
                [str(exe.resolve())] + (["preload", "windows-context"] if exe == args.after else []) + [str(initial_file.resolve()), str(frame_file.resolve())],
                capture_output=True, timeout=15)
            entries = [p.decode("utf-8") for p in process.stdout.split(b"\0") if p]
            observed.append({"returncode": process.returncode,
                             "entries": sorted(entries),
                             "stderr": process.stderr.decode("utf-8", "replace")})
        # Compare exact spelling and values, not Python's Unicode case folding.
        passed = observed[0] == observed[1] and observed[0]["returncode"] in (0, 94)
        report["cases"].append({"case": name, "passed": passed,
                                "before": observed[0], "after": observed[1]})
        print("CONTEXT_COMPARE", name, "PASS" if passed else "FAIL", flush=True)
    with (args.output / "results.json").open("w", encoding="utf-8", newline="\n") as file:
        json.dump(report, file, indent=2)
        file.write("\n")
    assert all(row["passed"] for row in report["cases"]), "context semantics differ"


if __name__ == "__main__":
    main()
