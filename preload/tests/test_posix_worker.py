#!/usr/bin/env python3
"""Verify real FIFO worker framing, context and ordered completion on POSIX."""
import os
import signal
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def frame(arguments, context=b""):
  """Encode the bounded argv frame followed by context fields."""
  return struct.pack("<I", len(arguments)) + b"git\0" + b"".join(
    argument.encode("utf-8") + b"\0" for argument in arguments) + context + b"end\0"


def environment_cases(git, inherited):
  """Return env-del/env-add cases shared by the POSIX and Windows worker tests.

  Parameters
  ----------
  git : str
      Worker executable, also run directly to compute expected results.
  inherited : dict
      Environment the worker is spawned with; it sets GIT_EDITOR.

  Returns
  -------
  list of tuple
      (name, arguments, context fields, exit status, stdout). Deletion and
      empty-value results are what git prints when run directly with the
      environment the worker should end up with.
  """
  def direct(arguments, changes, removed=()):
    environment = {k: v for k, v in inherited.items() if k not in removed}
    environment.update(changes)
    result = subprocess.run([git, *arguments], env=environment, capture_output=True)
    return result.returncode, result.stdout

  ident = {"GIT_AUTHOR_NAME": "Zoë=x", "GIT_AUTHOR_EMAIL": "z@example.com",
       "GIT_AUTHOR_DATE": "1700000000 +0000"}
  add_ident = "".join(f"{k}={v}\n" for k, v in ident.items()).encode("utf-8")
  editor = ["var", "GIT_EDITOR"]
  cases = [
    ("env-add-replaces", editor, b"env-add\0GIT_EDITOR=added-editor\n\0",
     0, b"added-editor\n"),
    ("env-add-unicode-and-equals", ["var", "GIT_AUTHOR_IDENT"],
     b"env-add\0" + add_ident + b"\0", 0,
     "Zoë=x <z@example.com> 1700000000 +0000\n".encode("utf-8")),
    # Deletions apply before additions whatever the wire order.
    ("env-del-then-add", editor,
     b"env-add\0GIT_EDITOR=again\n\0env-del\0GIT_EDITOR\n\0", 0, b"again\n"),
    ("env-fields-empty", ["--version"], b"env-del\0\0env-add\0\0",
     0, subprocess.check_output([git, "--version"])),
  ]
  cases.append(("env-del-removes", editor, b"env-del\0GIT_EDITOR\n\0")
         + direct(editor, {}, ("GIT_EDITOR",)))
  # Commands git starts (here a shell alias running git) see the changes.
  child = ["-c", "alias.child-editor=!git var GIT_EDITOR", "child-editor"]
  cases.append(("env-add-reaches-children", child,
         b"env-add\0GIT_EDITOR=added-editor\n\0", 0, b"added-editor\n"))
  # The alias's git is the first on PATH, so the direct run uses the alias too.
  cases.append(("env-del-reaches-children", child, b"env-del\0GIT_EDITOR\n\0")
         + direct(child, {}, ("GIT_EDITOR",)))
  cases.append(("env-add-empty-value", editor, b"env-add\0GIT_EDITOR=\n\0")
         + direct(editor, {"GIT_EDITOR": ""}))
  # Malformed fields are rejected before any change, like invalid argv.
  for name, context in (
      ("env-add-missing-newline", b"env-add\0GIT_EDITOR=x\0"),
      ("env-add-empty-name", b"env-add\0=x\n\0"),
      ("env-add-without-equals", b"env-add\0GIT_EDITOR\n\0"),
      ("env-del-with-equals", b"env-del\0GIT_EDITOR=x\n\0"),
      ("env-del-empty-entry", b"env-del\0\n\0"),
      ("env-add-invalid-utf8", b"env-add\0GIT_EDITOR=\xff\n\0"),
      ("env-add-duplicate-field", b"env-add\0A=1\n\0env-add\0B=2\n\0")):
    cases.append((name, editor, context, 128, b""))
  return cases


def request_timeout(signum, frame):
  """Interrupt a stalled FIFO operation so fixture cleanup can kill its worker.

  Parameters
  ----------
  signum : int
      SIGALRM delivered after the ten-second case deadline.
  frame : frame
      Interrupted Python stack frame, supplied by the signal module.
  """
  raise TimeoutError("preload worker FIFO case exceeded ten seconds")


def run_case(git, name, payload, status, output, hold_request=False, stdin_data=None):
  """Send one FIFO request and require output EOF before status consumption.

  With stdin_data, the bytes are written to the stdin FIFO concurrently with
  output draining, then it closes; otherwise it closes at once (end-of-file).
  """
  with tempfile.TemporaryDirectory(prefix="preload-worker-") as directory:
    environment = os.environ.copy()
    environment["PRELOAD_PID"] = str(os.getpid())
    environment["GIT_EDITOR"] = "preload-worker-inherited-editor"
    worker = subprocess.Popen([git, "--preload-worker", directory], env=environment,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    streams = []
    worker_directory = Path(directory) / f"worker-{worker.pid}"
    previous = signal.signal(signal.SIGALRM, request_timeout)
    signal.alarm(10)
    try:
      assert worker.stdout.readline() == f"READY {worker.pid}\n".encode()
      # Open in the worker's order; each FIFO open waits for the worker's.
      for channel, mode in (("params-cwd", "wb"), ("stdin", "wb"), ("stdout", "rb"),
                  ("stderr", "rb"), ("exit-code", "rb")):
        streams.append(open(worker_directory / channel, mode, buffering=0))
      request, stdin, stdout, stderr, exit_code = streams
      if stdin_data is None:
        stdin.close()
      request.write(payload)
      if not hold_request:
        request.close()
      if stdin_data is not None:
        import threading

        def feed():
          """Write the input while output drains, then signal EOF."""
          try:
            stdin.write(stdin_data)
          finally:
            stdin.close()

        threading.Thread(target=feed, daemon=True).start()
      assert stdout.read() == output, name
      errors = stderr.read()
      if status == 0:
        assert errors == b"", (name, errors)
      assert exit_code.read() == f"EXIT {status}\n".encode(), name
      assert worker.wait(timeout=5) == status, name
      assert not worker_directory.exists(), "worker retained FIFO endpoints"
      print("PRELOAD_POSIX_WORKER_PASS", name, flush=True)
    finally:
      signal.alarm(0)
      signal.signal(signal.SIGALRM, previous)
      for stream in streams:
        stream.close()
      if worker.poll() is None:
        worker.kill()
      worker.wait(timeout=5)
      worker.stdout.close()
      worker.stderr.close()


def main():
  """Check valid commands and malformed frames through the native entry point."""
  assert os.name != "nt"
  git = str(Path(sys.argv[1]).resolve())
  version = subprocess.check_output([git, "--version"])
  run_case(git, "version-with-context-writer-held", frame(["--version"]),
       0, version, hold_request=True)
  run_case(git, "stdin-eof", frame(["hash-object", "--stdin"]), 0,
       subprocess.check_output([git, "hash-object", "--stdin"], input=b""))
  run_case(git, "literal-end-argument", frame(["check-ref-format", "refs/heads/end"]), 0, b"")
  run_case(git, "short-header", b"\0", 124, b"")
  run_case(git, "missing-executable", struct.pack("<I", 0), 124, b"")
  run_case(git, "excess-parameters", struct.pack("<I", 65536), 124, b"")
  run_case(git, "high-bit-argc", struct.pack("<I", 0x80000000), 124, b"")
  run_case(git, "maximum-argc", struct.pack("<I", 0xffffffff), 124, b"")
  run_case(git, "unterminated-argv", struct.pack("<I", 0) + b"git", 124, b"")
  run_case(git, "wrong-program", struct.pack("<I", 0) + b"other\0end\0", 128, b"")
  run_case(git, "malformed-context", frame(["--version"], b"unknown\0"), 124, b"")
  run_case(git, "unsupported-environment-field", frame(["--version"], b"env\0"),
       124, b"")
  run_case(git, "unsupported-export-field", frame(["--version"], b"env-export\0"),
       124, b"")
  run_case(git, "environment-inherited-from-spawn",
       frame(["var", "GIT_EDITOR"]), 0, b"preload-worker-inherited-editor\n")
  for bad in (b"\xff", b"\xc0\x80", b"\xed\xa0\x80"):
    run_case(git, "invalid-utf8-argv-" + bad.hex(),
         struct.pack("<I", 1) + b"git\0" + bad + b"\0end\0", 128, b"")
  inherited = dict(os.environ, PRELOAD_PID=str(os.getpid()),
           GIT_EDITOR="preload-worker-inherited-editor")
  for name, arguments, context, status, output in environment_cases(git, inherited):
    run_case(git, name, frame(arguments, context), status, output)
  # The caller's stdin FIFO is the command's stdin.
  for name, data in (("stdin-text", b"hello\n"), ("stdin-empty", b""),
             ("stdin-binary-nul", bytes(range(256)) * 4),
             ("stdin-exceeds-pipe-buffer", b"x" * (1024 * 1024 + 7))):
    expected = subprocess.check_output([git, "hash-object", "--stdin"], input=data)
    run_case(git, name, frame(["hash-object", "--stdin"]), 0, expected, stdin_data=data)
  data = b"preload stdin line\n" * 60000
  run_case(git, "stdin-and-stdout-exceed-pipe-buffers", frame(["stripspace"]),
       0, subprocess.check_output([git, "stripspace"], input=data), stdin_data=data)


if __name__ == "__main__":
  main()
