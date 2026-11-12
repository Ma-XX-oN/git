#!/usr/bin/env python3
"""Generic preload manager for Windows.

Clients connect to the duplex named pipe `<pipe-dir>\\request` once per call
and send one line on that connection:

  PRELOAD <name> <native-executable-path>   ->  OK | ERROR <reason>
  GET <name>                                ->  eight hexadecimal PID bytes, no newline
                                                or 00000000 + ERROR <reason> + newline
  GET <name> <marker> + worker request      ->  as GET <name>
  STOP                                      ->  OK, then the manager exits

The client rebuilds the worker's pipe directory `<pipe-dir>\\worker-<pid>`
from the PID. A fixed-length reply lets Bash read it in one call; a line
reply costs Bash one read per byte on a pipe.

With a marker digit, the GET line is followed by exactly the bytes the client
would otherwise write to the worker's `params-cwd` channel. After the reply
the manager writes the marker to that channel and relays the following bytes
unparsed until the client closes the request connection, then holds the
channel until the worker exits. The client opens only stdout, stderr,
exit-code and stdin. PRELOAD_CONTEXT_CONNECT selects when the manager
connects: `get` (default) when forwarding, `spawn` as soon as the worker is
READY.

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
import queue
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
K.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD,
                          W.HANDLE]
K.CreateFileW.restype = W.HANDLE

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
GENERIC_READ_WRITE = 0xc0000000
OPEN_EXISTING = 3


def connect_context(pipe_dir, pid):
  """Connect to a worker's params-cwd channel.

  The worker accepts a connection made before it reaches that channel; the
  marker byte written later identifies it.

  Parameters
  ----------
  pipe_dir : str
      Session pipe directory.
  pid : int
      Worker PID.

  Returns
  -------
  int
      Owned pipe handle.
  """
  address = f"{pipe_dir}\\worker-{pid}\\params-cwd"
  for _ in range(40):
    handle = K.CreateFileW(address, GENERIC_READ_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle != INVALID:
      return handle
    K.WaitNamedPipeW(address, 5)
  raise C.WinError(C.get_last_error())


def write_all(handle, payload):
  """Write a complete payload to a pipe.

  Parameters
  ----------
  handle : int
      Borrowed connected pipe handle.
  payload : bytes
      Bytes to write.
  """
  count = W.DWORD()
  if not K.WriteFile(handle, payload, len(payload), C.byref(count), None) or \
      count.value != len(payload):
    raise C.WinError(C.get_last_error())


class Server:
  """Accept per-call connections on the session's request pipe.

  Parameters
  ----------
  address : str
      Full request pipe name, `<pipe-dir>\\request`.
  pipe_dir : str
      Session pipe directory, used to reach a worker's params-cwd channel.
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
    """Answer one request, then wait for the client to close.

    Parameters
    ----------
    handle : int
        Borrowed connected instance.
    """
    request = self._read_line(handle)
    if request is None:
      return  # Git Bash's throwaway connection, or an aborted client.
    line, data = request
    received = time.time()
    command, _, rest = line.partition(" ")
    worker = None
    forward = None
    if command == "GET":
      name, _, marker = rest.partition(" ")
      allocated = time.time()
      if marker and (len(marker) != 1 or not marker.isdigit()):
        reply = "00000000ERROR invalid marker"
      else:
        result = self.manager.allocate(name)
        allocated = time.time()
        if isinstance(result, str):
          reply = "00000000" + result
        else:
          worker = result
          reply = f"{worker.pid:08x}"
          if marker:
            forward = marker.encode("ascii") + data
          else:
            self._release_context(worker)  # The client connects params-cwd itself.
    elif command == "PRELOAD":
      name, _, path = rest.partition(" ")
      reply = self.manager.preload(name, path) if name and path else "ERROR usage"
    elif command == "STOP":
      reply = "OK"
    else:
      reply = "ERROR unknown request"
    try:
      write_all(handle, (reply if worker is not None else reply + "\n").encode("utf-8"))
    except OSError:
      if worker is not None:  # The client never learned of this worker.
        worker.kill()
        self._release_context(worker)
        forward = None
      raise
    finally:
      # Start the replacement only now, so it cannot delay the reply.
      if worker is not None:
        self.manager.replenish(name)
    if command == "GET" and preload_pool.TIMING:
      log(f"TIMING GET received={received:.6f} allocated={allocated:.6f} "
          f"delivered={time.time():.6f}")
    if command == "STOP":
      log("MANAGER_STOP_REQUEST")
      self.stopped.set()
    if forward is not None:
      self._forward(handle, worker, forward)
    # Disconnecting discards unread data; wait until the client closes.
    self._drain(handle)

  def _read_line(self, handle):
    """Read one newline-terminated request.

    Returns
    -------
    tuple of (str, bytes) or None
        Line without newline and any bytes already read after it, or None
        if the client closed first.
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
    end = data.index(b"\n")
    return data[:end].decode("utf-8"), bytes(data[end + 1:])

  def _drain(self, handle):
    """Discard request bytes until the client closes its connection.

    Parameters
    ----------
    handle : int
        Borrowed connected instance.
    """
    buffer = C.create_string_buffer(MAX_LINE)
    count = W.DWORD()
    while K.ReadFile(handle, buffer, MAX_LINE, C.byref(count), None) and count.value:
      pass

  def _forward(self, request, worker, head):
    """Relay the rest of the request connection to the worker's params-cwd.

    The bytes after the GET line are exactly what params-cwd expects; they
    are relayed unparsed until the client closes the request connection,
    which it does after reading the reply. The worker validates them as it
    would the client's own write.

    Reading the request never waits on params-cwd: the worker reads that
    channel only after the client has connected the others, and the client
    does so only after writing its whole request. A request larger than the
    params-cwd buffer would otherwise deadlock.

    Parameters
    ----------
    request : int
        Borrowed connected request instance.
    worker : subprocess.Popen
        Allocated worker; its preconnected handle, if any, is used.
    head : bytes
        Marker digit followed by the request bytes already read.
    """
    context = getattr(worker, "preload_context", None)
    worker.preload_context = None
    try:
      if context is None:
        context = connect_context(self.pipe_dir, worker.pid)
      # At most MAX_LINE + 1 bytes into an empty 64 KiB buffer: cannot block.
      write_all(context, head)
    except OSError as error:
      log(f"FORWARD_FAILED pid={worker.pid} {error!r}")
      worker.kill()
      if context is not None:
        K.CloseHandle(context)
      return
    chunks = queue.SimpleQueue()
    threading.Thread(target=self._deliver, args=(worker, context, chunks),
                     daemon=True).start()
    buffer = C.create_string_buffer(MAX_LINE)
    count = W.DWORD()
    while K.ReadFile(request, buffer, MAX_LINE, C.byref(count), None) and count.value:
      chunks.put(buffer.raw[:count.value])
    chunks.put(None)
    if preload_pool.TIMING:
      log(f"TIMING FORWARD pid={worker.pid} relayed={time.time():.6f}")

  @staticmethod
  def _deliver(worker, context, chunks):
    """Write relayed request chunks, then hold params-cwd until the worker exits.

    Closing the connection before the worker reaches that channel would
    discard the marker and request.

    Parameters
    ----------
    worker : subprocess.Popen
        Allocated worker.
    context : int
        Owned params-cwd handle, closed here.
    chunks : queue.SimpleQueue
        Request chunks in order, ended by None.
    """
    try:
      while (chunk := chunks.get()) is not None:
        write_all(context, chunk)
      worker.wait()
    except OSError as error:
      log(f"FORWARD_FAILED pid={worker.pid} {error!r}")
      worker.kill()
    finally:
      K.CloseHandle(context)

  @staticmethod
  def _release_context(worker):
    """Close a preconnected params-cwd handle that will not be used.

    The worker treats the closed connection as a probe and keeps waiting for
    the client's own connection.

    Parameters
    ----------
    worker : subprocess.Popen
        Allocated worker, possibly holding a preconnected handle.
    """
    handle = getattr(worker, "preload_context", None)
    worker.preload_context = None
    if handle is not None:
      K.CloseHandle(handle)


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
  prepare = None
  if os.environ.get("PRELOAD_CONTEXT_CONNECT") == "spawn":
    def prepare(worker):
      """Preconnect a READY worker's params-cwd channel for forwarding."""
      worker.preload_context = connect_context(args.pipe_dir, worker.pid)
  manager = preload_pool.Manager(directory, preload_pool.worker_environment(
    args.owner_pid, {"PRELOAD_WIN_DIR": args.pipe_dir}), prepare)
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
