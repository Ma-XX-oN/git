"""Drop-in `subprocess` facade that runs `git` through a preload session.

Import it in place of the standard module::

  import preload_subprocess as subprocess

  subprocess.run(["git", "status"], capture_output=True, text=True)
  subprocess.check_output(["git", "rev-parse", "HEAD"], env_add={"GIT_DIR": d})

`run()`, `check_output()` and `check_call()` send a command whose first
argument is exactly ``"git"`` to a worker of the preload session inherited
from the environment (``PRELOAD_PID`` and ``PRELOAD_DIR``, plus
``PRELOAD_WIN_DIR`` on Windows), as exported by a shell that sourced
`manager.sh` and ran ``preload git``. Every other command, and every other
name in this module, is the standard `subprocess` one.

Git calls keep the standard arguments, results and exceptions: ``args``,
``stdin``, ``input``, ``stdout``, ``stderr``, ``capture_output``, ``check``,
``timeout``, ``cwd``, ``text``, ``universal_newlines``, ``encoding`` and
``errors``. Two arguments are added, for all commands:

* ``env_add``: mapping of variables to set for this call only.
* ``env_del``: iterable of variable names to remove for this call only.

A worker inherits the session's environment, so ``env=`` (a complete
environment) is rejected for git; use the two deltas instead. Names cannot be
empty or contain ``=``, newline or NUL; values cannot contain newline or NUL.
Deletions apply before additions. Any other `Popen` argument is rejected for
git, and so is a file or descriptor as ``stdin``: git's input is ``input=``
or nothing. ``stdin=None``, ``PIPE`` and ``DEVNULL`` give git end-of-file.
Output inherited from this process (``stdout=None``/``stderr=None``) is copied
to descriptors 1 and 2. Git sees pipes, never a terminal. Preload failures
raise `PreloadError`, an `OSError`, as a failed launch would.

Importing raises `PreloadError` when no session is exported.
"""
import errno as _errno
import itertools as _itertools
import json as _json
import locale as _locale
import os as _os
import random as _random
import signal as _signal
import subprocess as _subprocess
import sys as _sys
import threading as _threading
import time as _timing

_WINDOWS = _os.name == "nt"

#: Arguments a git call accepts besides those of run() itself.
_GIT_OPTIONS = frozenset({"stdin", "stdout", "stderr", "cwd", "text", "universal_newlines",
              "encoding", "errors", "env", "env_add", "env_del"})
#: Largest request the worker accepts, in bytes.
_REQUEST_CAPACITY = 131072
#: Largest parameter count, excluding "git".
_MAX_PARAMETERS = 65535


class PreloadError(OSError):
  """The preload session cannot serve a git call (raised like a launch failure)."""


def __getattr__(name):
  """Forward every other attribute (including private ones) to `subprocess`.

  Looked up on each access, so replacing an attribute of the real module (for
  example by a test) is seen here too. This module defines only run(),
  check_output(), check_call(), PreloadError and underscore-prefixed names
  that `subprocess` does not have.
  """
  return getattr(_subprocess, name)


def __dir__():
  """List this module's names together with those of `subprocess`."""
  return sorted(set(globals()) | set(dir(_subprocess)))


#: Star-imports take the standard names (resolved via __getattr__) and ours.
__all__ = list(_subprocess.__all__) + ["PreloadError"]


def _is_git(args):
  """Return whether args is a command list whose first item is exactly "git"."""
  return isinstance(args, (list, tuple)) and len(args) > 0 and \
    isinstance(args[0], str) and args[0] == "git"


def _command(popenargs, kwargs):
  """Return the args of a call, given positionally or as args=."""
  return popenargs[0] if popenargs else kwargs.get("args")


def _environment_for_delegate(kwargs):
  """Turn env_add/env_del into a complete env= for the standard subprocess.

  Parameters
  ----------
  kwargs : dict
      Call arguments, modified in place.
  """
  add = kwargs.pop("env_add", None)
  delete = kwargs.pop("env_del", None)
  if add is None and delete is None:
    return kwargs
  if kwargs.get("env") is not None:
    raise ValueError("env cannot be combined with env_add or env_del")
  _check_names(delete or (), "env_del")
  _check_assignments(add or {})
  fold = str.upper if _WINDOWS else str
  environment = {fold(k): v for k, v in _os.environ.items()}
  for name in delete or ():
    environment.pop(fold(name), None)
  for name, value in (add or {}).items():
    environment[fold(name)] = value
  kwargs["env"] = environment
  return kwargs


def _check_names(names, option):
  """Raise ValueError unless every name is usable in env_del or env_add."""
  for name in names:
    if not isinstance(name, str) or not name or any(c in name for c in "=\n\0"):
      raise ValueError(f"{option}: invalid variable name {name!r}")


def _check_assignments(add):
  """Raise ValueError unless every env_add name and value is usable."""
  _check_names(add.keys(), "env_add")
  for name, value in add.items():
    if not isinstance(value, str) or "\n" in value or "\0" in value:
      raise ValueError(f"env_add: invalid value for {name!r}")


def _default_text_encoding():
  """Return the default text encoding the standard subprocess would use."""
  return "utf-8" if _sys.flags.utf8_mode else _locale.getencoding()


def _translate_newlines(data, encoding, errors):
  """Decode output and translate newlines exactly as subprocess does."""
  return data.decode(encoding, errors or "strict").replace("\r\n", "\n").replace("\r", "\n")


def _write_all(fd, data):
  """Write every byte of data to a descriptor."""
  view = memoryview(data)
  while view:
    view = view[_os.write(fd, view):]


# Session channels (manager connections and worker pipes) use three
# primitives: _channel_read, _channel_write and _channel_close. On POSIX they
# are FIFO descriptors. On Windows they are native pipe handles: the C
# runtime reports a pipe whose server end has gone (ERROR_PIPE_NOT_CONNECTED)
# as EINVAL, indistinguishable from other errors, so Win32 is called directly.
if _WINDOWS:
  import ctypes as _ctypes
  from ctypes import wintypes as _wintypes

  _kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
  _kernel32.CreateFileW.argtypes = [_wintypes.LPCWSTR, _wintypes.DWORD, _wintypes.DWORD,
                    _wintypes.LPVOID, _wintypes.DWORD, _wintypes.DWORD,
                    _wintypes.HANDLE]
  _kernel32.CreateFileW.restype = _wintypes.HANDLE
  _kernel32.ReadFile.argtypes = [_wintypes.HANDLE, _wintypes.LPVOID, _wintypes.DWORD,
                   _ctypes.POINTER(_wintypes.DWORD), _wintypes.LPVOID]
  _kernel32.ReadFile.restype = _wintypes.BOOL
  _kernel32.WriteFile.argtypes = [_wintypes.HANDLE, _wintypes.LPCVOID, _wintypes.DWORD,
                  _ctypes.POINTER(_wintypes.DWORD), _wintypes.LPVOID]
  _kernel32.WriteFile.restype = _wintypes.BOOL
  _kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
  _kernel32.CloseHandle.restype = _wintypes.BOOL
  _INVALID_HANDLE = _ctypes.c_void_p(-1).value
  #: ERROR_BROKEN_PIPE and ERROR_PIPE_NOT_CONNECTED: the other end has gone.
  _PIPE_ENDED = (109, 233)

  def _channel_open(path):
    """Open a named pipe read/write; return its handle or raise OSError."""
    handle = _kernel32.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
    if handle == _INVALID_HANDLE:
      raise _ctypes.WinError(_ctypes.get_last_error())
    return handle

  def _channel_read(handle, size):
    """Read up to size bytes; b"" once the other end has gone."""
    buffer = _ctypes.create_string_buffer(size)
    count = _wintypes.DWORD()
    if not _kernel32.ReadFile(handle, buffer, size, _ctypes.byref(count), None):
      error = _ctypes.get_last_error()
      if error in _PIPE_ENDED:
        return b""
      raise _ctypes.WinError(error)
    return buffer.raw[:count.value]

  def _channel_write(handle, data):
    """Write every byte; a vanished reader raises BrokenPipeError."""
    data = bytes(data)
    offset = 0
    while offset < len(data):
      count = _wintypes.DWORD()
      chunk = data[offset:]
      if not _kernel32.WriteFile(handle, chunk, len(chunk), _ctypes.byref(count), None):
        raise _ctypes.WinError(_ctypes.get_last_error())
      offset += count.value

  def _channel_close(handle):
    """Close a pipe handle, ignoring failure."""
    _kernel32.CloseHandle(handle)
else:
  _channel_read = _os.read
  _channel_write = _write_all

  def _channel_close(fd):
    """Close a FIFO descriptor, ignoring failure."""
    try:
      _os.close(fd)
    except OSError:
      pass


def _fileno(target, name):
  """Return the descriptor of an int or file-like stream argument."""
  if isinstance(target, int) and target >= 0:
    return target
  if hasattr(target, "fileno"):
    return target.fileno()
  raise ValueError(f"unsupported {name} value for preloaded git: {target!r}")


class _Sink:
  """Destination of one output stream: captured, discarded or a descriptor."""

  def __init__(self, target, inherited_fd, name):
    """Build the sink for a stdout/stderr argument.

    Parameters
    ----------
    target : None, PIPE, DEVNULL, int or file
        The standard subprocess argument.
    inherited_fd : int
        Descriptor that None (inherit) writes to.
    name : str
        Argument name for error messages.
    """
    self.chunks = None
    self.fd = None
    if target is None:
      self.fd = inherited_fd
    elif target == _subprocess.PIPE:
      self.chunks = []
    elif target != _subprocess.DEVNULL:
      self.fd = _fileno(target, name)

  def write(self, data):
    """Deliver one chunk of output."""
    if self.chunks is not None:
      self.chunks.append(data)
    elif self.fd is not None:
      _write_all(self.fd, data)

  def value(self):
    """Return the captured bytes, or None when not captured."""
    return None if self.chunks is None else b"".join(self.chunks)


class _Request:
  """One validated git call: wire frame, input and output destinations."""

  def __init__(self, args, kwargs, input):
    """Validate the arguments of a git call.

    Parameters
    ----------
    args : list or tuple
        Command whose first item is "git".
    kwargs : dict
        Remaining Popen-style arguments (already without run()'s own).
    input : bytes, str or None
        Data for stdin; requires stdin to be None or PIPE.
    """
    unsupported = sorted(set(kwargs) - _GIT_OPTIONS)
    if unsupported:
      raise ValueError(f"{unsupported[0]}= is not supported for preloaded git")
    if kwargs.get("env") is not None:
      raise ValueError("env= is not supported for preloaded git; use env_add= and env_del=")
    text, universal = kwargs.get("text"), kwargs.get("universal_newlines")
    if text is not None and universal is not None and bool(text) != bool(universal):
      raise _subprocess.SubprocessError("Cannot disambiguate when both text and universal_newlines "
                  "are supplied but different. Pass one or the other.")
    self.encoding, self.errors = kwargs.get("encoding"), kwargs.get("errors")
    self.text = bool(self.encoding or self.errors or text or universal)
    if self.text and self.encoding is None:
      self.encoding = _default_text_encoding()
    self.args = args
    self.frame = self._frame(args, kwargs)
    self._input(kwargs.get("stdin"), input)
    self.stdout = _Sink(kwargs.get("stdout"), 1, "stdout")
    stderr = kwargs.get("stderr")
    self.stderr = self.stdout if stderr == _subprocess.STDOUT else _Sink(stderr, 2, "stderr")

  @staticmethod
  def _frame(args, kwargs):
    """Encode argv, cwd and environment changes as the worker request."""
    words = []
    for word in args[1:]:
      word = _os.fspath(word)
      word = word.encode("utf-8") if isinstance(word, str) else bytes(word)
      if b"\0" in word:
        raise ValueError("embedded null byte")
      words.append(word)
    if len(words) > _MAX_PARAMETERS:
      raise OSError(_errno.E2BIG, f"preloaded git accepts at most {_MAX_PARAMETERS} parameters")
    cwd = kwargs.get("cwd")
    cwd = _os.getcwd() if cwd is None else _os.path.abspath(_os.fsdecode(_os.fspath(cwd)))
    if not _os.path.isdir(cwd):
      if _WINDOWS:
        raise NotADirectoryError(_errno.ENOTDIR, "The directory name is invalid", cwd)
      raise FileNotFoundError(_errno.ENOENT, _os.strerror(_errno.ENOENT), cwd)
    frame = [len(words).to_bytes(4, "little"), b"git\0"]
    frame += [word + b"\0" for word in words]
    frame.append(b"cwd\0" + cwd.encode("utf-8") + b"\n\0")
    delete, add = kwargs.get("env_del"), kwargs.get("env_add")
    if delete:
      delete = list(delete)
      _check_names(delete, "env_del")
      frame.append(b"env-del\0" + "".join(f"{n}\n" for n in delete).encode("utf-8") + b"\0")
    if add:
      _check_assignments(add)
      frame.append(b"env-add\0" + "".join(f"{n}={v}\n" for n, v in add.items())
             .encode("utf-8") + b"\0")
    frame.append(b"end\0")
    frame = b"".join(frame)
    if len(frame) > _REQUEST_CAPACITY:
      raise OSError(_errno.E2BIG, "preloaded git request exceeds 128 KiB")
    return frame

  def _input(self, stdin, input):
    """Set input_bytes, the data to send, or None for end-of-file.

    A file or descriptor as stdin is rejected: the tests never pass one.
    """
    if stdin not in (None, _subprocess.PIPE, _subprocess.DEVNULL):
      raise ValueError("stdin= must be None, PIPE or DEVNULL for preloaded git; "
               "pass data with input=")
    self.input_bytes = None
    if input is not None:
      if self.text:
        if _WINDOWS:
          input = input.replace("\n", "\r\n")  # TextIOWrapper's write translation.
        input = input.encode(self.encoding, self.errors or "strict")
      self.input_bytes = bytes(memoryview(input))

  def result(self, sink):
    """Return a sink's captured output, decoded in text mode.

    Undecodable output raises UnicodeDecodeError on POSIX. On Windows the
    standard module decodes in a reader thread that dies on the error, so
    run() returns None for that stream; this matches it.
    """
    data = sink.value()
    if data is None or not self.text:
      return data
    try:
      return _translate_newlines(data, self.encoding, self.errors)
    except UnicodeDecodeError:
      if _WINDOWS:
        return None
      raise


class _Call:
  """Descriptors of one allocated worker; owned until the call ends."""

  def __init__(self, pid, stdin, stdout, stderr, status, request=None):
    self.pid = pid
    self.stdin, self.stdout, self.stderr, self.status = stdin, stdout, stderr, status
    self.request = request  # Windows: the GET connection held for the call.

  def close_channels(self):
    """Close every worker channel still open."""
    for name in ("stdin", "stdout", "stderr", "status"):
      fd = getattr(self, name)
      if fd is not None:
        setattr(self, name, None)
        _channel_close(fd)


def _read_exact(fd, size):
  """Read up to size bytes, stopping early only at end-of-file."""
  data = b""
  while len(data) < size:
    chunk = _channel_read(fd, size - len(data))
    if not chunk:
      break
    data += chunk
  return data


def _read_line(fd):
  """Read one reply line without its newline; empty at immediate end-of-file."""
  data = bytearray()
  while True:
    byte = _channel_read(fd, 1)
    if not byte or byte == b"\n":
      return data.decode("utf-8", "replace")
    data += byte


def _read_pid(fd):
  """Read a GET reply: eight hex PID digits, or 00000000 and an error line."""
  reply = _read_exact(fd, 8)
  if reply == b"00000000":
    raise PreloadError(_errno.EIO, f"preload: {_read_line(fd) or 'no reply'}")
  if len(reply) != 8 or any(c not in b"0123456789abcdef" for c in reply):
    raise PreloadError(_errno.EIO, "preload: no reply from the preload manager")
  return int(reply, 16)


class _Session:
  """The inherited preload session; platform subclasses implement transport."""

  @staticmethod
  def from_environment(environ):
    """Return the session exported in environ, or raise PreloadError."""
    names = ("PRELOAD_PID", "PRELOAD_WIN_DIR") if _WINDOWS else ("PRELOAD_PID", "PRELOAD_DIR")
    missing = [name for name in names if not environ.get(name)]
    if missing:
      raise PreloadError(_errno.ENOENT, "no preload session: " + ", ".join(missing) +
                 " not set (source manager.sh and run `preload git` first)")
    return _WindowsSession(environ) if _WINDOWS else _PosixSession(environ)

  def execute(self, request, timeout):
    """Run one git call; return (returncode, stdout, stderr) as run() would.

    Raises TimeoutExpired after killing the worker when timeout expires.

    Threads that read or write a channel own it until they finish; a channel
    is closed only after its thread has ended, so its handle can never be
    reused under a running thread.
    """
    call = self._allocate(request.frame)
    ended = False
    threads, failures = [], []

    def start(target, *arguments):
      def body():
        try:
          target(*arguments)
        except BaseException as error:  # Re-raised by the caller below.
          failures.append(error)
      thread = _threading.Thread(target=body, daemon=True)
      thread.start()
      threads.append(thread)

    try:
      stdin, call.stdin = call.stdin, None  # Owned by the input writer, or closed now.
      if request.input_bytes:
        start(self._feed, stdin, request.input_bytes)
      else:
        _channel_close(stdin)
      start(self._drain, call.stderr, request.stderr)
      if timeout is None:
        self._drain(call.stdout, request.stdout)
        for thread in threads:
          thread.join()
      else:
        start(self._drain, call.stdout, request.stdout)
        deadline = _timing.monotonic() + timeout
        for thread in threads:
          thread.join(max(0, deadline - _timing.monotonic()))
        if any(thread.is_alive() for thread in threads):
          self._kill(call.pid)
          for thread in threads:
            thread.join()  # Channels close as the worker dies.
          ended = True
          self._end(call, lost=False)
          raise self._timeout(request, timeout)
      if failures:
        raise failures[0]
      status = _read_exact(call.status, 64)
      call.close_channels()
      ended = True
      if status.startswith(b"EXIT ") and status.endswith(b"\n") and status[5:-1].isdigit():
        self._end(call, lost=False)
        code = int(status[5:-1])
      elif not status:
        code = self._end(call, lost=True)  # The worker died mid-command.
      else:
        self._end(call, lost=False)
        raise PreloadError(_errno.EIO, f"preload: invalid worker exit status {status!r}")
      return code, request.result(request.stdout), \
        None if request.stderr is request.stdout else request.result(request.stderr)
    except BaseException:
      if not ended:
        # As run() does on any exception (including KeyboardInterrupt): kill
        # the child, then release the call once its I/O threads have ended.
        self._kill(call.pid)
        for thread in threads:
          thread.join(5)
        if not any(thread.is_alive() for thread in threads):
          call.close_channels()
        self._end(call, lost=False)
      raise

  @staticmethod
  def _timeout(request, timeout):
    """Build the TimeoutExpired that run() raises, with the output so far."""
    if _WINDOWS:  # run() collects the rest with communicate() after kill().
      out = request.result(request.stdout)
      err = None if request.stderr is request.stdout else request.result(request.stderr)
    else:  # POSIX run() reports the raw bytes read before the deadline.
      out = request.stdout.value()
      err = None if request.stderr is request.stdout else request.stderr.value()
    return _subprocess.TimeoutExpired(request.args, timeout, output=out, stderr=err)

  @staticmethod
  def _drain(fd, sink):
    """Copy one borrowed output channel to its sink until end-of-file."""
    while True:
      data = _channel_read(fd, 65536)
      if not data:
        return
      sink.write(data)

  @staticmethod
  def _feed(stdin, data):
    """Write input to the owned stdin channel, then close it (EOF).

    Write failures after the command stops reading are ignored, as
    communicate() ignores them.
    """
    try:
      _channel_write(stdin, data)
    except OSError as error:
      if not isinstance(error, BrokenPipeError) and \
         error.errno not in (_errno.EINVAL, _errno.EPIPE):
        raise
    finally:
      _channel_close(stdin)

  @staticmethod
  def _kill(pid):
    """Kill a worker as Popen.kill() would kill a child."""
    try:
      _os.kill(pid, _signal.SIGTERM if _WINDOWS else _signal.SIGKILL)
    except OSError:
      pass

  @staticmethod
  def _status_code(reply):
    """Map a manager STATUS reply to a returncode, as Popen reports it."""
    word, _, number = reply.partition(" ")
    if word == "EXIT" and number.isdigit():
      return int(number)
    if word == "SIGNAL" and number.isdigit():
      return -int(number)
    raise PreloadError(_errno.EIO, f"preload: no exit status: {reply or 'no reply'}")


class _WindowsSession(_Session):
  """Named-pipe transport: a held GET connection per call, marked channels."""

  def __init__(self, environ):
    self.pipe_dir = environ["PRELOAD_WIN_DIR"]
    self.sequence = _itertools.count(1)

  @staticmethod
  def _open(path, attempts=40):
    """Open a named pipe read/write, retrying while instances are busy."""
    for attempt in range(attempts):
      try:
        return _channel_open(path)
      except OSError as error:
        if attempt + 1 == attempts:
          raise PreloadError(error.errno, f"preload: cannot open {path}: {error.strerror}")
        _timing.sleep(.005)

  def _allocate(self, frame):
    """Send GET with the request and connect the worker's four channels."""
    marker = str(next(self.sequence) % 10).encode("ascii")
    request = self._open(self.pipe_dir + "\\request")
    channels = []
    try:
      _channel_write(request, b"GET git " + marker + b"\n" + frame)
      pid = _read_pid(request)
      base = f"{self.pipe_dir}\\worker-{pid}\\"
      for name in ("stdin", "stdout", "stderr", "exit-code"):
        channels.append(self._open(base + name))
        _channel_write(channels[-1], marker)
      # One ACK byte once the worker has every marker; EOF if it ended.
      _channel_read(channels[0], 1)
    except BaseException:
      for fd in channels + [request]:
        _channel_close(fd)
      raise
    return _Call(pid, *channels, request=request)

  def _end(self, call, lost):
    """End the call by closing the GET connection; ask STATUS first if lost."""
    try:
      if not lost:
        return None
      try:
        _channel_write(call.request, b"STATUS\n")
        reply = _read_line(call.request)
      except OSError:
        reply = ""
      return self._status_code(reply)
    finally:
      _channel_close(call.request)


class _PosixSession(_Session):
  """FIFO transport: mailbox replies and per-call worker FIFOs."""

  def __init__(self, environ):
    self.directory = environ["PRELOAD_DIR"]
    try:
      with open(_os.path.join(self.directory, "ready.json"), encoding="utf-8") as ready:
        state = _json.load(ready)
      self.slots, self.manager_pid = int(state["slots"]), int(state["manager_pid"])
    except (OSError, ValueError, KeyError) as error:
      raise PreloadError(_errno.ENOENT, f"no preload session in {self.directory}: {error}")
    self.request_fd = None
    self.lock = _threading.Lock()

  def _send(self, line):
    """Write one request line (atomic below PIPE_BUF) to the manager."""
    with self.lock:
      if self.request_fd is None:
        try:
          # Non-blocking open fails at once if the manager has exited.
          fd = _os.open(_os.path.join(self.directory, "request"), _os.O_WRONLY | _os.O_NONBLOCK)
        except OSError as error:
          raise PreloadError(error.errno, "preload: the preload manager stopped")
        _os.set_blocking(fd, True)
        self.request_fd = fd
    try:
      _write_all(self.request_fd, line.encode("utf-8"))
    except OSError as error:
      raise PreloadError(error.errno, "preload: the preload manager stopped")

  def _claim(self):
    """Claim a free mailbox by exclusively creating its lock with our PID."""
    while True:
      if not _os.path.exists(_os.path.join(self.directory, "ready.json")):
        raise PreloadError(_errno.EIO, "preload: the preload manager stopped")
      start = _random.randrange(self.slots)
      for index in range(self.slots):
        slot = (start + index) % self.slots
        try:
          fd = _os.open(_os.path.join(self.directory, f"slot.{slot}.lock"),
                 _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
        except FileExistsError:
          continue
        _os.write(fd, str(_os.getpid()).encode("ascii"))
        _os.close(fd)
        return slot
      _timing.sleep(.001)  # Every mailbox is busy; wait for a FREE.

  def _exchange(self, line, reader):
    """Send line (with {slot}) and read its reply from a claimed mailbox."""
    slot = self._claim()
    try:
      try:
        _os.kill(self.manager_pid, 0)
      except OSError:
        raise PreloadError(_errno.EIO, "preload: the preload manager stopped")
      # The manager holds the only writer, so this open does not block.
      mailbox = _os.open(_os.path.join(self.directory, f"slot.{slot}"), _os.O_RDONLY)
      try:
        self._send(line.format(slot=slot))
        return reader(mailbox)
      finally:
        _os.close(mailbox)
    finally:
      self._send(f"FREE {slot}\n")

  def _allocate(self, frame):
    """GET a worker, open its five FIFOs in order and send the request."""
    pid = self._exchange("GET git {slot}\n", _read_pid)
    base = _os.path.join(self.directory, f"worker-{pid}")
    channels = []
    try:
      for name, mode in (("params-cwd", _os.O_WRONLY), ("stdin", _os.O_WRONLY),
                 ("stdout", _os.O_RDONLY), ("stderr", _os.O_RDONLY),
                 ("exit-code", _os.O_RDONLY)):
        channels.append(_os.open(_os.path.join(base, name), mode))
      _write_all(channels[0], frame)
      _os.close(channels.pop(0))
    except BaseException:
      for fd in channels:
        _os.close(fd)
      self._send(f"DONE {pid}\n")
      raise
    return _Call(pid, *channels)

  def _end(self, call, lost):
    """Send DONE, or DONE ... STATUS and return the dead worker's code."""
    if not lost:
      self._send(f"DONE {call.pid}\n")
      return None
    return self._status_code(self._exchange(f"DONE {call.pid} {{slot}} STATUS\n", _read_line))


_SESSION = _Session.from_environment(_os.environ)


def _run_git(args, kwargs, input, timeout):
  """Run a validated git call; return (returncode, stdout, stderr)."""
  return _SESSION.execute(_Request(args, kwargs, input), timeout)


def run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
  """`subprocess.run`, with git served by the preload session.

  Accepts env_add= and env_del= for every command; see the module docstring.
  """
  args = _command(popenargs, kwargs)
  if not _is_git(args):
    return _subprocess.run(*popenargs, input=input, capture_output=capture_output,
                 timeout=timeout, check=check, **_environment_for_delegate(kwargs))
  if len(popenargs) > 1:
    raise ValueError("positional Popen arguments are not supported for preloaded git")
  kwargs.pop("args", None)
  if input is not None and kwargs.get("stdin") is not None:
    raise ValueError("stdin and input arguments may not both be used.")
  if capture_output:
    if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
      raise ValueError("stdout and stderr arguments may not be used with capture_output.")
    kwargs["stdout"] = kwargs["stderr"] = _subprocess.PIPE
  returncode, stdout, stderr = _run_git(args, kwargs, input, timeout)
  if check and returncode:
    raise _subprocess.CalledProcessError(returncode, args, output=stdout, stderr=stderr)
  return _subprocess.CompletedProcess(args, returncode, stdout, stderr)


def check_output(*popenargs, timeout=None, **kwargs):
  """`subprocess.check_output`, with git served by the preload session."""
  if not _is_git(_command(popenargs, kwargs)):
    return _subprocess.check_output(*popenargs, timeout=timeout,
                    **_environment_for_delegate(kwargs))
  for kw in ("stdout", "check"):
    if kw in kwargs:
      raise ValueError(f"{kw} argument not allowed, it will be overridden.")
  if "input" in kwargs and kwargs["input"] is None:
    # As in subprocess: an explicit input=None means empty input.
    text = kwargs.get("universal_newlines") or kwargs.get("text") or \
      kwargs.get("encoding") or kwargs.get("errors")
    kwargs["input"] = "" if text else b""
  return run(*popenargs, stdout=_subprocess.PIPE, timeout=timeout, check=True, **kwargs).stdout


def check_call(*popenargs, timeout=None, **kwargs):
  """`subprocess.check_call`, with git served by the preload session."""
  args = _command(popenargs, kwargs)
  if not _is_git(args):
    return _subprocess.check_call(*popenargs, timeout=timeout,
                   **_environment_for_delegate(kwargs))
  if len(popenargs) > 1:
    raise ValueError("positional Popen arguments are not supported for preloaded git")
  kwargs.pop("args", None)
  for kw in ("input", "capture_output", "check"):
    if kw in kwargs:  # call() passes these to Popen, which rejects them.
      raise TypeError(f"Popen.__init__() got an unexpected keyword argument '{kw}'")
  returncode = _run_git(args, kwargs, None, timeout)[0]
  if returncode:
    raise _subprocess.CalledProcessError(returncode, args)
  return 0
