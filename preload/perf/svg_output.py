"""Stream Matplotlib SVG output without trailing whitespace in path data."""

import io
import re


class SvgWriter:
  """Filter Matplotlib's double-quoted path attributes one line at a time.

  Parameters
  ----------
  destination : text file-like object
      Borrowed writable stream. The caller owns and closes it, and must call
      ``finish()`` after the SVG export. Only an unfinished line is buffered.
      This writer targets Matplotlib's serialization, not arbitrary SVG XML.
  """

  _path_start = re.compile(r'<path\b[^>]*\bd="')

  def __init__(self, destination):
    """Initialize the borrowed destination and empty line/path state."""
    self.destination = destination
    self.pending = ""
    self.in_path = False

  def write(self, text):
    """Consume a text chunk, forwarding complete lines immediately.

    Parameters
    ----------
    text : str
        SVG fragment; writes may split anywhere, including inside tags.

    Returns
    -------
    int
        Number of input characters consumed, including removed spaces.
    """
    start = 0
    while True:
      end = text.find("\n", start)
      if end < 0:
        self.pending += text[start:]
        break
      self._emit(self.pending + text[start:end], newline="\n")
      self.pending = ""
      start = end + 1
    return len(text)

  def _emit(self, line, newline=""):
    """Write a line, trimming only while its end is inside path data.

    Parameters
    ----------
    line : str
        Complete line without its newline, or the final partial line.
    newline : str
        LF for a complete line; empty for the final partial line.

    Returns
    -------
    None
        Updates path state and writes to the borrowed destination.
    """
    if self.in_path:
      self.in_path = '"' not in line
    else:
      match = self._path_start.search(line)
      if match:
        self.in_path = '"' not in line[match.end():]
    if self.in_path:
      line = line.rstrip(" \t")
    self.destination.write(line + newline)

  def flush(self):
    """Flush the destination, retaining the unfinished line.

    Returns
    -------
    None
        Further writes can still complete the buffered line.
    """
    self.destination.flush()

  def seek(self, offset, whence=0):
    """Reject repositioning on this forward-only text stream.

    Parameters
    ----------
    offset : int
        Requested character offset; all values are unsupported.
    whence : int, optional
        Requested origin; all values are unsupported.

    Raises
    ------
    io.UnsupportedOperation
        Streaming cleanup cannot revisit previously written output.
    """
    raise io.UnsupportedOperation("SVG output is forward-only")

  def finish(self):
    """Write the final partial line and flush without closing the stream.

    Returns
    -------
    None
        The caller retains ownership of the destination.
    """
    if self.pending:
      self._emit(self.pending)
      self.pending = ""
    self.flush()
