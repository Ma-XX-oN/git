#!/usr/bin/env python3
"""Compare preload_subprocess git calls with the standard subprocess module.

Run --git PATH. The outer process starts a Bash session (source manager.sh;
preload git) and reruns this file inside it with --inner, as a test harness
would. Each case runs the same command through the facade (["git", ...],
served by a worker) and through the standard module (the git executable run
directly) and requires identical return codes, output and exceptions. A guard
on subprocess.Popen fails any facade git call that is not preloaded.
PRELOAD_MANAGER selects the manager (default: the built preload-manager).
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"

# Windows helper run by a git alias: end the nearest git.exe ancestor with
# STATUS_ACCESS_VIOLATION, the code of a real MinGW Git crash. (POSIX aliases
# use `kill -9 $PPID`.)
KILL_GIT_ANCESTOR = r'''
import ctypes
import os
from ctypes import wintypes as W

k = ctypes.WinDLL("kernel32", use_last_error=True)


class Entry(ctypes.Structure):
  _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
        ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", W.DWORD),
        ("szExeFile", ctypes.c_wchar * 260)]


k.CreateToolhelp32Snapshot.restype = W.HANDLE
k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(Entry)]
k.OpenProcess.restype = W.HANDLE
k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
snapshot = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
entry = Entry()
entry.dwSize = ctypes.sizeof(Entry)
parents = {}
found = k.Process32FirstW(snapshot, ctypes.byref(entry))
while found:
  parents[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
  found = k.Process32NextW(snapshot, ctypes.byref(entry))
pid = parents[os.getpid()][0]
while parents[pid][1] != "git.exe":
  pid = parents[pid][0]
k.TerminateProcess(k.OpenProcess(1, False, pid), 0xC0000005)  # PROCESS_TERMINATE
'''


def shell_path(path):
  """Return a path usable by the test's Bash."""
  if WINDOWS:
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()
  return str(path)


def outer(git):
  """Start a preload session in Bash and run the inner checks inside it."""
  with tempfile.TemporaryDirectory(prefix="preload-subprocess-") as work:
    work = Path(work)
    (work / "empty.gitconfig").write_bytes(b"")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
           GIT_CONFIG_GLOBAL=str(work / "empty.gitconfig"),
           GIT_EDITOR="session-editor")
    env["PYTHONPATH"] = os.pathsep.join(
      [str(ROOT / "preload/client")] + [p for p in [env.get("PYTHONPATH")] if p])
    manager = ROOT / ("preload-manager.exe" if WINDOWS else "preload-manager")
    if "PRELOAD_MANAGER" not in env and manager.exists():
      env["PRELOAD_MANAGER"] = str(manager)
    script = ('source "$1" || exit 90; preload "$2" || exit 91; '
          '"$3" "$4" --inner --git "$5" --work "$6"')
    command = [shutil.which("bash"), "--noprofile", "--norc", "-c", script, "facade",
           shell_path(ROOT / "preload/client/manager.sh"), shell_path(git),
           shell_path(sys.executable), str(Path(__file__).resolve()), str(git), str(work)]
    result = subprocess.run(command, env=env, timeout=600)
    if result.returncode:
      sys.exit(f"inner checks failed with status {result.returncode}")
  print("PRELOAD_SUBPROCESS_TESTS_PASS", flush=True)


def inner(git, work):
  """Run every comparison inside the inherited preload session."""
  import preload_subprocess as facade

  real = subprocess
  original_popen = real.Popen

  class Guard(original_popen):
    """Fail if a facade git call reaches the real Popen."""
    def __init__(self, args, *rest, **kwargs):
      assert not facade._is_git(args), f"git was not preloaded: {args!r}"
      super().__init__(args, *rest, **kwargs)

  real.Popen = Guard
  # Forwarding is live: the facade sees the replaced attribute.
  assert facade.Popen is Guard and facade.PIPE is real.PIPE
  assert facade.CalledProcessError is real.CalledProcessError
  assert facade._args_from_interpreter_flags is real._args_from_interpreter_flags
  real.PRELOAD_FACADE_SENTINEL = sentinel = object()
  assert facade.PRELOAD_FACADE_SENTINEL is sentinel and "Popen" in dir(facade)
  del real.PRELOAD_FACADE_SENTINEL
  passed("forwards-to-subprocess")

  repo = work / "repo"
  real.run([git, "init", "-q", str(repo)], check=True)
  real.run([git, "-C", str(repo), "-c", "user.name=a", "-c", "user.email=a@b",
        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
  config = work / "values.config"
  config.write_bytes(b"[x]\nbad = a\xffb\n[large]\nvalue = " + b"y" * (1024 * 1024) + b"\n")

  def same(name, through_facade, direct):
    """Require identical outcomes from a facade call and a direct call."""
    a, b = outcome(through_facade), outcome(direct)
    assert a == b, (name, a, b)
    passed(name)

  G = git
  same("version", lambda: facade.run(["git", "--version"], capture_output=True),
     lambda: real.run([G, "--version"], capture_output=True))
  same("text", lambda: facade.run(["git", "--version"], capture_output=True, text=True),
     lambda: real.run([G, "--version"], capture_output=True, text=True))
  same("encoding-errors", lambda: facade.run(["git", "config", "--file", str(config), "x.bad"],
                         capture_output=True, encoding="utf-8",
                         errors="replace"),
     lambda: real.run([G, "config", "--file", str(config), "x.bad"], capture_output=True,
              encoding="utf-8", errors="replace"))
  same("undecodable-text-raises",
     lambda: facade.run(["git", "config", "--file", str(config), "x.bad"],
              capture_output=True, encoding="utf-8"),
     lambda: real.run([G, "config", "--file", str(config), "x.bad"],
              capture_output=True, encoding="utf-8"))
  same("universal-newlines-translate",
     lambda: facade.run(["git", "var", "GIT_EDITOR"], capture_output=True,
              universal_newlines=True, env_add={"GIT_EDITOR": "a\rb"}),
     lambda: real.run([G, "var", "GIT_EDITOR"], capture_output=True,
              universal_newlines=True, env=dict(os.environ, GIT_EDITOR="a\rb")))
  data = bytes(range(256)) * 64
  same("input-bytes", lambda: facade.run(["git", "hash-object", "--stdin"], input=data,
                       capture_output=True),
     lambda: real.run([G, "hash-object", "--stdin"], input=data, capture_output=True))
  same("input-text", lambda: facade.run(["git", "hash-object", "--stdin"],
                      input="one\ntwo\n", capture_output=True, text=True),
     lambda: real.run([G, "hash-object", "--stdin"], input="one\ntwo\n",
              capture_output=True, text=True))
  same("stdin-none-is-eof", lambda: facade.run(["git", "hash-object", "--stdin"],
                         capture_output=True),
     lambda: real.run([G, "hash-object", "--stdin"], stdin=real.DEVNULL,
              capture_output=True))
  same("stdin-pipe-without-input", lambda: facade.run(["git", "hash-object", "--stdin"],
                            stdin=facade.PIPE, capture_output=True),
     lambda: real.run([G, "hash-object", "--stdin"], stdin=real.PIPE, capture_output=True))
  lines = b"preload stdin line\n" * 60000
  same("large-input-and-output", lambda: facade.run(["git", "stripspace"], input=lines,
                            capture_output=True),
     lambda: real.run([G, "stripspace"], input=lines, capture_output=True))
  same("large-output", lambda: facade.run(["git", "config", "--file", str(config),
                       "large.value"], capture_output=True),
     lambda: real.run([G, "config", "--file", str(config), "large.value"],
              capture_output=True))
  same("stderr-to-stdout", lambda: facade.run(["git", "rev-parse", "--verify", "missing"],
                        cwd=repo, stdout=facade.PIPE, stderr=facade.STDOUT),
     lambda: real.run([G, "rev-parse", "--verify", "missing"], cwd=repo,
              stdout=real.PIPE, stderr=real.STDOUT))
  same("nonzero-exit", lambda: facade.run(["git", "rev-parse", "--verify", "missing"],
                       cwd=repo, capture_output=True),
     lambda: real.run([G, "rev-parse", "--verify", "missing"], cwd=repo,
              capture_output=True))
  same("check-raises", lambda: facade.run(["git", "rev-parse", "--verify", "missing"],
                       cwd=repo, capture_output=True, check=True),
     lambda: real.run([G, "rev-parse", "--verify", "missing"], cwd=repo,
              capture_output=True, check=True))
  same("no-capture-results-none", lambda: facade.run(["git", "--version"],
                            stdout=facade.DEVNULL),
     lambda: real.run([G, "--version"], stdout=real.DEVNULL))
  previous = os.getcwd()
  os.chdir(work)
  try:
    same("cwd-relative", lambda: facade.run(["git", "rev-parse", "--show-toplevel"],
                        cwd="repo", capture_output=True),
       lambda: real.run([G, "rev-parse", "--show-toplevel"], cwd="repo",
                capture_output=True))
    same("cwd-default", lambda: facade.run(["git", "rev-parse", "--is-inside-work-tree"],
                         capture_output=True),
       lambda: real.run([G, "rev-parse", "--is-inside-work-tree"], capture_output=True))
  finally:
    os.chdir(previous)
  same("cwd-missing", lambda: facade.run(["git", "--version"], cwd=work / "missing"),
     lambda: real.run([G, "--version"], cwd=work / "missing"))
  ident = {"GIT_AUTHOR_NAME": "Zoë=x", "GIT_AUTHOR_EMAIL": "z@example.com",
       "GIT_AUTHOR_DATE": "1700000000 +0000"}
  same("env-add", lambda: facade.run(["git", "var", "GIT_AUTHOR_IDENT"], env_add=ident,
                     capture_output=True),
     lambda: real.run([G, "var", "GIT_AUTHOR_IDENT"], env=dict(os.environ, **ident),
              capture_output=True))
  without = {k: v for k, v in os.environ.items() if k.upper() != "GIT_EDITOR"}
  same("env-del", lambda: facade.run(["git", "var", "GIT_EDITOR"], env_del=["GIT_EDITOR"],
                     capture_output=True),
     lambda: real.run([G, "var", "GIT_EDITOR"], env=without, capture_output=True))
  same("check-output", lambda: facade.check_output(["git", "rev-parse", "HEAD"], cwd=repo),
     lambda: real.check_output([G, "rev-parse", "HEAD"], cwd=repo))
  same("check-output-explicit-input-none",
     lambda: facade.check_output(["git", "hash-object", "--stdin"], input=None, text=True),
     lambda: real.check_output([G, "hash-object", "--stdin"], input=None, text=True))
  same("check-output-stderr-devnull",
     lambda: facade.check_output(["git", "rev-parse", "--verify", "missing"], cwd=repo,
                   stderr=facade.DEVNULL),
     lambda: real.check_output([G, "rev-parse", "--verify", "missing"], cwd=repo,
                  stderr=real.DEVNULL))
  same("check-output-rejects-stdout",
     lambda: facade.check_output(["git", "--version"], stdout=facade.PIPE),
     lambda: real.check_output([G, "--version"], stdout=real.PIPE))
  same("check-call", lambda: facade.check_call(["git", "--version"], stdout=facade.DEVNULL),
     lambda: real.check_call([G, "--version"], stdout=real.DEVNULL))
  same("check-call-raises",
     lambda: facade.check_call(["git", "rev-parse", "--verify", "missing"], cwd=repo,
                  stderr=facade.DEVNULL),
     lambda: real.check_call([G, "rev-parse", "--verify", "missing"], cwd=repo,
                 stderr=real.DEVNULL))
  same("check-call-rejects-input", lambda: facade.check_call(["git", "--version"], input=b""),
     lambda: real.check_call([G, "--version"], input=b""))
  same("run-rejects-stdin-with-input",
     lambda: facade.run(["git", "--version"], stdin=facade.PIPE, input=b""),
     lambda: real.run([G, "--version"], stdin=real.PIPE, input=b""))
  same("run-rejects-capture-with-stdout",
     lambda: facade.run(["git", "--version"], capture_output=True, stdout=facade.PIPE),
     lambda: real.run([G, "--version"], capture_output=True, stdout=real.PIPE))
  result = facade.run(command := ["git", "--version"], capture_output=True)
  assert result.args is command and isinstance(result, real.CompletedProcess)
  passed("args-preserved")

  # Facade-only rules.
  for name, call in (("env-rejected", lambda: facade.run(["git", "--version"], env={})),
             ("unsupported-option-rejected",
              lambda: facade.run(["git", "--version"], shell=False)),
             ("bad-env-name", lambda: facade.run(["git", "--version"],
                               env_add={"A=B": "x"})),
             ("bad-env-value", lambda: facade.run(["git", "--version"],
                                env_add={"A": "x\ny"})),
             ("env-with-delta", lambda: facade.run([sys.executable, "-c", ""], env={},
                                 env_add={"A": "1"})),
             ("stdin-descriptor-rejected",
              lambda: facade.run(["git", "hash-object", "--stdin"], stdin=0))):
    try:
      call()
    except ValueError:
      passed(name)
    else:
      raise AssertionError(f"{name}: no ValueError")
  probe = "import os, sys; sys.stdout.write(os.environ.get('PRELOAD_FACADE_X', '-'))"
  assert facade.run([sys.executable, "-c", probe], env_add={"PRELOAD_FACADE_X": "y"},
            capture_output=True, text=True).stdout == "y"
  os.environ["PRELOAD_FACADE_X"] = "z"
  assert facade.check_output([sys.executable, "-c", probe], env_del=["PRELOAD_FACADE_X"],
                 text=True) == "-"
  del os.environ["PRELOAD_FACADE_X"]
  passed("other-commands-use-subprocess-with-deltas")

  # Inherited output goes to descriptors 1 and 2, as a child's would.
  for fd, words in ((1, ["--version"]), (2, ["rev-parse", "--verify", "missing"])):
    captured = []
    for runner, head in ((facade.run, "git"), (real.run, G)):
      with tempfile.TemporaryFile() as sink:
        saved = os.dup(fd)
        os.dup2(sink.fileno(), fd)
        try:
          runner([head, *words], cwd=repo)
        finally:
          os.dup2(saved, fd)
          os.close(saved)
        sink.seek(0)
        captured.append(sink.read())
    assert captured[0] == captured[1] and captured[0], (fd, captured)
  passed("inherited-stdout-and-stderr")

  # A command that outlasts its timeout is killed; the session keeps serving.
  # The alias's sleep does not hold git's pipes, so they close when git dies.
  nap = ["-c", "alias.nap=!sleep 5 >/dev/null 2>&1 </dev/null", "nap"]
  for runner, head in ((facade.run, "git"), (real.run, G)):
    try:
      runner([head, *nap], capture_output=True, timeout=1)
    except real.TimeoutExpired as error:
      assert error.timeout == 1
    else:
      raise AssertionError("no TimeoutExpired")
  same("after-timeout", lambda: facade.run(["git", "--version"], capture_output=True),
     lambda: real.run([G, "--version"], capture_output=True))
  passed("timeout")

  # A worker killed mid-command reports the code a killed child would: the
  # manager supplies it when the exit-code channel closes without a record.
  if WINDOWS:
    killer = work / "kill_git_ancestor.py"
    killer.write_text(KILL_GIT_ANCESTOR, encoding="utf-8")
    die = ["-c", f'alias.die=!"{shell_path(sys.executable)}" "{shell_path(killer)}"', "die"]
  else:
    die = ["-c", "alias.die=!kill -9 $PPID", "die"]
  same("worker-killed-mid-command", lambda: facade.run(["git", *die], capture_output=True),
     lambda: real.run([G, *die], capture_output=True))
  killed = facade.run(["git", *die], capture_output=True).returncode
  assert killed == (0xC0000005 if WINDOWS else -9), killed
  same("after-worker-killed", lambda: facade.run(["git", "--version"], capture_output=True),
     lambda: real.run([G, "--version"], capture_output=True))

  expected = real.run([G, "--version"], capture_output=True).stdout
  results, errors = [], []

  def worker():
    try:
      for _ in range(20):
        results.append(facade.run(["git", "--version"], capture_output=True).stdout)
    except BaseException as error:
      errors.append(error)

  threads = [threading.Thread(target=worker) for _ in range(8)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(120)
  assert not errors and results == [expected] * 160, (errors, len(results))
  passed("concurrent-calls")
  real.Popen = original_popen


def outcome(call):
  """Return a comparable summary of a call's result or exception."""
  try:
    result = call()
  except Exception as error:
    return ("raised", type(error).__name__, getattr(error, "returncode", None),
        getattr(error, "output", None), getattr(error, "stderr", None),
        getattr(error, "errno", None))
  if isinstance(result, subprocess.CompletedProcess):
    return ("returned", result.returncode, result.stdout, result.stderr)
  return ("returned", result)


def passed(name):
  """Report one passing case."""
  print("PRELOAD_SUBPROCESS_PASS", name, flush=True)


def main():
  """Dispatch to the outer session launcher or the inner checks."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", type=Path, required=True)
  parser.add_argument("--inner", action="store_true")
  parser.add_argument("--work", type=Path)
  args = parser.parse_args()
  git = str(args.git.resolve(strict=True))
  if args.inner:
    inner(git, args.work)
  else:
    outer(git)


if __name__ == "__main__":
  main()
