#!/usr/bin/env python3
"""Generic preload manager for Windows.

Clients connect to the duplex named pipe `<pipe-dir>\\request` once per call
and send one line on that connection:

  PRELOAD <name> <native-executable-path>   ->  OK | ERROR <reason>
  GET <name>                                ->  <pipe-dir>\\worker-<pid> | ERROR <reason>
  STOP                                      ->  OK, then the manager exits

Each connection is a private reply channel, so concurrent subshells never see
each other's replies. Git Bash opens a throwaway connection before the real
one (https://github.com/Ma-XX-oN/git/issues/7); it closes without sending
data and is simply disconnected. At least MIN_LISTENING instances are always
listening, so the real open never finds the pipe busy.
"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preload_pool  # noqa: E402
from preload_pool import log  # noqa: E402

if os.name != "nt":
  raise RuntimeError("Windows preload manager requires native Windows Python")

K = C.WinDLL("kernel32", use_last_error=True)
K.CreateNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
                               W.DWORD, W.DWORD, W.LPVOID]
K.CreateNamedPipeW.restype = W.HANDLE
K.ConnectNamedPipe.argtypes = [W.HANDLE, W.LPVOID]
K.ConnectNamedPipe.restype = W.BOOL
K.DisconnectNamedPipe.argtypes = [W.HANDLE]
K.DisconnectNamedPipe.restype = W.BOOL
K.ReadFile.argtypes = [W.HANDLE, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
K.ReadFile.restype = W.BOOL
K.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID]
K.WriteFile.restype = W.BOOL
K.FlushFileBuffers.argtypes = [W.HANDLE]
K.FlushFileBuffers.restype = W.BOOL
K.CloseHandle.argtypes = [W.HANDLE]
K.CloseHandle.restype = W.BOOL
K.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
K.OpenProcess.restype = W.HANDLE
K.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
K.WaitForSingleObject.restype = W.DWORD
K.WaitNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD]
K.WaitNamedPipeW.restype = W.BOOL

INVALID = C.c_void_p(-1).value
PIPE_ACCESS_DUPLEX = 3
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE = 0
PIPE_READMODE_BYTE = 0
PIPE_WAIT = 0
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_CONNECTED = 535
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 258
MIN_LISTENING = 3
MAX_IDLE = 6
MAX_LINE = 4096


class Server:
  """Accept per-call connections on the session's request pipe.

  Parameters
  ----------
  address : str
      Full request pipe name, `<pipe-dir>\\request`.
  pipe_dir : str
      Session pipe directory used to build worker replies.
  manager : preload_pool.Manager
      Pools serving PRELOAD and GET requests.
  """

  def __init__(self, address, pipe_dir, manager):
    self.address = address
    self.pipe_dir = pipe_dir
    self.manager = manager
    self.lock = threading.Lock()
    self.listening = 0
    self.idle = 0
    self.stopped = threading.Event()
    self.first = True

  def start(self):
    """Create the first instances; the first claims the name exclusively."""
    for _ in range(MIN_LISTENING):
      self._start_listener()

  def _create(self):
    """Create one pipe instance.

    Returns
    -------
    int
        Owned pipe handle.
    """
    with self.lock:
      first, self.first = self.first, False
    handle = K.CreateNamedPipeW(
      self.address,
      PIPE_ACCESS_DUPLEX | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
      PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
      PIPE_UNLIMITED_INSTANCES, MAX_LINE, MAX_LINE, 0, None)
    if handle == INVALID:
      raise C.WinError(C.get_last_error())
    return handle

  def _start_listener(self):
    """Start one thread that owns one instance for its lifetime."""
    handle = self._create()
    with self.lock:
      self.listening += 1
    threading.Thread(target=self._listen, args=(handle,), daemon=True).start()

  def _listen(self, handle):
    """Serve connections on one instance until it is no longer needed.

    Parameters
    ----------
    handle : int
        Owned pipe instance; closed when the thread exits.
    """
    try:
      while not self.stopped.is_set():
        if not K.ConnectNamedPipe(handle, None) and \
            C.get_last_error() != ERROR_PIPE_CONNECTED:
          with self.lock:
            self.listening -= 1
          log("CONNECT_FAILED " + str(C.get_last_error()))
          return
        with self.lock:
          self.listening -= 1
          short = self.listening < MIN_LISTENING
        if short and not self.stopped.is_set():
          self._start_listener()
        try:
          self._serve(handle)
        except Exception as error:  # One failed client must not stop the server.
          log("REQUEST_FAILED " + repr(error))
        finally:
          K.DisconnectNamedPipe(handle)
        with self.lock:
          if self.listening >= MAX_IDLE:
            return
          self.listening += 1
    finally:
      K.CloseHandle(handle)

  def _serve(self, handle):
    """Answer one request line, then wait for the client to close.

    Parameters
    ----------
    handle : int
        Borrowed connected instance.
    """
    line = self._read_line(handle)
    if line is None:
      return  # Git Bash's throwaway connection, or an aborted client.
    received = time.time()
    command, _, rest = line.partition(" ")
    worker = None
    if command == "GET":
      result = self.manager.allocate(rest)
      allocated = time.time()
      if isinstance(result, str):
        reply = result
      else:
        worker = result
        reply = f"{self.pipe_dir}\\worker-{worker.pid}"
    elif command == "PRELOAD":
      name, _, path = rest.partition(" ")
      reply = self.manager.preload(name, path) if name and path else "ERROR usage"
    elif command == "STOP":
      reply = "OK"
    else:
      reply = "ERROR unknown request"
    try:
      self._write(handle, (reply + "\n").encode("utf-8"))
    except OSError:
      if worker is not None:  # The client never learned of this worker.
        worker.kill()
      raise
    finally:
      # Start the replacement only now, so it cannot delay the reply.
      if worker is not None:
        self.manager.replenish(rest)
    if command == "GET" and preload_pool.TIMING:
      log(f"TIMING GET received={received:.6f} allocated={allocated:.6f} "
          f"delivered={time.time():.6f}")
    if command == "STOP":
      log("MANAGER_STOP_REQUEST")
      self.stopped.set()
    # Disconnecting discards unread data; wait until the client closes.
    self._read_line(handle)

  def _read_line(self, handle):
    """Read one newline-terminated request.

    Returns
    -------
    str or None
        Line without newline, or None if the client closed first.
    """
    data = bytearray()
    buffer = C.create_string_buffer(MAX_LINE)
    count = W.DWORD()
    while b"\n" not in data:
      if not K.ReadFile(handle, buffer, MAX_LINE, C.byref(count), None) or \
          not count.value:
        return None
      data.extend(buffer.raw[:count.value])
      if len(data) > MAX_LINE:
        return None
    return data[:data.index(b"\n")].decode("utf-8")

  def _write(self, handle, payload):
    """Write a complete reply.

    Parameters
    ----------
    handle : int
        Borrowed connected instance.
    payload : bytes
        Reply line including its newline.
    """
    count = W.DWORD()
    if not K.WriteFile(handle, payload, len(payload), C.byref(count), None) or \
        count.value != len(payload):
      raise C.WinError(C.get_last_error())


def main():
  """Run the manager until STOP or the owner process exits."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--owner-pid", required=True, type=int)
  parser.add_argument("--directory", required=True, type=Path)
  parser.add_argument("--pipe-dir", required=True)
  args = parser.parse_args()
  directory = args.directory.resolve(strict=True)
  owner = K.OpenProcess(SYNCHRONIZE, False, args.owner_pid)
  if not owner:
    raise OSError(C.get_last_error(), "OpenProcess(owner)")
  manager = preload_pool.Manager(directory, preload_pool.worker_environment(
    args.owner_pid, {"PRELOAD_WIN_DIR": args.pipe_dir}))
  address = args.pipe_dir + "\\request"
  server = Server(address, args.pipe_dir, manager)
  ready = directory / "ready.json"
  try:
    server.start()
    if not K.WaitNamedPipeW(address, 5000):
      raise RuntimeError("request pipe not available")
    temporary = ready.with_suffix(".tmp")
    temporary.write_text(json.dumps({
      "state": "READY", "manager_pid": os.getpid(), "owner_pid": args.owner_pid,
      "pipe_dir": args.pipe_dir}) + "\n", encoding="utf-8")
    temporary.replace(ready)
    log("READY " + address)
    while not server.stopped.is_set():
      if K.WaitForSingleObject(owner, 250) != WAIT_TIMEOUT:
        log(f"OWNER_EXIT {args.owner_pid}")
        break
  finally:
    server.stopped.set()
    ready.unlink(missing_ok=True)
    manager.close()
    preload_pool.cleanup_session(directory)
    K.CloseHandle(owner)


if __name__ == "__main__":
  sys.exit(main())
