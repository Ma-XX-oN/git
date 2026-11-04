#!/usr/bin/env python3
"""RWF #809 investigation of installed Git executable startup, not RWF."""
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

N = 120

def invoke(argv, env=None):
  t = time.perf_counter_ns()
  proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=env, check=False, timeout=20)
  return ((time.perf_counter_ns() - t) / 1e6, proc.returncode,
          proc.stdout.decode("utf-8", errors="replace").strip())

def measure(argv, n=N, env=None):
  for _ in range(5):
    ms, code, _ = invoke(argv, env)
    if code != 0:
      raise RuntimeError("warmup command failed")
  values = []
  for _ in range(n):
    ms, code, _ = invoke(argv, env)
    if code != 0:
      raise RuntimeError("measurement command failed")
    values.append(ms)
  a = sorted(values)
  return {"median_ms": statistics.median(a), "p95_ms": a[int(.95*(len(a)-1))],
          "min_ms": a[0], "max_ms": a[-1], "samples_ms": values}

def build_noop(temp):
  source = temp / "noop.c"
  source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
  output = temp / ("noop.exe" if os.name == "nt" else "noop")
  if os.name != "nt":
    cc = shutil.which("cc") or shutil.which("clang")
    if not cc:
      return None, "no C compiler found"
    proc = subprocess.run([cc, "-O2", "-o", str(output), str(source)],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (output, "compiled with cc") if proc.returncode == 0 else (None, proc.stderr[:500])
  where = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
  if not where.is_file():
    return None, "vswhere unavailable"
  proc = subprocess.run([str(where), "-latest", "-products", "*", "-property",
                          "installationPath"], text=True, capture_output=True)
  if proc.returncode != 0 or not proc.stdout.strip():
    return None, "Visual Studio installation unavailable"
  dev = Path(proc.stdout.strip()) / "Common7/Tools/VsDevCmd.bat"
  if not dev.is_file():
    return None, "VsDevCmd unavailable"
  compile_script = temp / "compile-noop.cmd"
  compile_script.write_text(
    '@echo off\r\n'
    + 'call "' + str(dev) + '" -arch=x64 >nul\r\n'
    + 'if errorlevel 1 exit /b 1\r\n'
    + 'cl /nologo /O2 /Fe:"' + str(output) + '" "' + str(source) + '" >nul\r\n',
    encoding="utf-8"
  )
  proc = subprocess.run(["cmd.exe", "/d", "/c", str(compile_script)],
                        capture_output=True, text=True)
  return (output, "compiled with Visual Studio cl") if proc.returncode == 0 and output.exists() else (None, "compiler failed: " + proc.stderr[:500])

def build_windows_dll_probes(temp):
  if os.name != "nt":
    return {}, "not Windows"
  source = Path(__file__).with_name("load_git_dlls.c")
  if not source.is_file():
    return {}, "source missing"
  where = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
  if not where.is_file():
    return {}, "vswhere missing"
  lookup = subprocess.run([str(where), "-latest", "-products", "*", "-property",
                           "installationPath"], text=True, capture_output=True)
  if lookup.returncode or not lookup.stdout.strip():
    return {}, "Visual Studio unavailable"
  dev = Path(lookup.stdout.strip()) / "Common7/Tools/VsDevCmd.bat"
  outputs = {"git_dlls_load": temp / "loadlibs.exe",
             "git_set_dll_directory": temp / "setdll.exe"}
  cmds = ['@echo off', 'call "' + str(dev) + '" -arch=x64 >nul',
          'if errorlevel 1 exit /b 11']
  for label, binary in outputs.items():
    define = "/DONLY_SETDIR " if label == "git_set_dll_directory" else ""
    cmds.extend(['cl /nologo /O2 ' + define + '/Fe:"' + str(binary)
                 + '" "' + str(source) + '" >nul',
                 'if errorlevel 1 exit /b 12'])
  script = temp / "compile-loaders.cmd"
  script.write_text(chr(10).join(cmds) + chr(10), encoding="utf-8")
  result = subprocess.run(["cmd.exe", "/d", "/c", str(script)],
                          capture_output=True, text=True)
  if result.returncode or any(not p.is_file() for p in outputs.values()):
    return {}, "DLL probe compiler failed: " + result.stderr[:500]
  return outputs, "compiled Windows DLL loader controls"


def dll_imports(exe):
  if os.name != "nt":
    return None
  try:
    import pefile
    pe = pefile.PE(str(exe), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    libraries = sorted({ent.dll.decode("ascii", "replace").lower()
                       for ent in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])})
    pe.close()
    return libraries
  except Exception as exc:
    return {"error": type(exc).__name__}

def main():
  git = shutil.which("git")
  if not git:
    raise RuntimeError("no Git on PATH")
  git = str(Path(git).resolve())
  ms, code, version = invoke([git, "--version"])
  if code != 0:
    raise RuntimeError("Git --version failed")
  data = {"source_head": os.environ.get("GITHUB_SHA", ""),
          "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "platform": platform.platform(), "python": sys.version,
          "git": git, "git_version": version, "cpu_count": os.cpu_count(),
          "measurement": {}, "controls": {}, "pe_imports": dll_imports(git)}
  with tempfile.TemporaryDirectory(prefix="git809-") as td:
    temp = Path(td)
    noop, compile_info = build_noop(temp)
    data["controls"]["noop_compile"] = compile_info
    candidates = {"native_git_version": [git, "--version"],
                  "python_nop": [sys.executable, "-c", "pass"]}
    if noop is not None:
      candidates["minimal_c_executable"] = [str(noop)]
      data["controls"]["noop_imports"] = dll_imports(noop)
    if os.name == "nt":
      loaders, loader_info = build_windows_dll_probes(temp)
      data["controls"]["dll_probe_compile"] = loader_info
      for label, binary in loaders.items():
        candidates[label] = [str(binary), str(Path(git).parent)]
      root = Path(git).parents[2] if "ucrt64" in git.lower() or "mingw64" in git.lower() else Path(git).parent.parent
      for label, relative in (("cmd_wrapper", "cmd/git.exe"), ("bin_wrapper", "bin/git.exe")):
        other = root / relative
        if other.exists() and str(other).lower() != git.lower():
          candidates[label] = [str(other), "--version"]
          data["controls"][label + "_imports"] = dll_imports(other)
    else:
      true = shutil.which("true")
      if true:
        candidates["system_true"] = [true]
    # Rotate command order to reduce sequential ordering bias.
    raw = {key: [] for key in candidates}
    for i in range(N):
      keys = list(candidates)
      if i % 2:
        keys.reverse()
      for key in keys:
        elapsed, returncode, _ = invoke(candidates[key])
        if returncode:
          raise RuntimeError("nonzero result in " + key)
        raw[key].append(elapsed)
    for key, values in raw.items():
      seq = sorted(values)
      data["measurement"][key] = {
        "median_ms": statistics.median(seq), "p95_ms": seq[int(.95*(len(seq)-1))],
        "samples_ms": values}
  output = Path("git809-results")
  output.mkdir(exist_ok=True)
  target = output / (platform.system().lower() + ".json")
  target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
  print(json.dumps({k:v for k,v in data.items() if k != "measurement"}, indent=2))
  print("MEDIANS", json.dumps({k:{"median_ms": v["median_ms"], "p95_ms": v["p95_ms"]}
                               for k,v in data["measurement"].items()}, indent=2))
  print("RESULT", target)

if __name__ == "__main__":
  main()
