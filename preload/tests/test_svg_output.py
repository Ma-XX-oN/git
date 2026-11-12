"""Check streaming SVG path cleanup without altering displayed text."""

import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "perf"))
from svg_output import SvgWriter


class SvgWriterTests(unittest.TestCase):
  """Exercise write boundaries, flushing and text preservation."""

  def test_every_write_boundary(self):
    """Preserve text and path separators at every two-chunk split."""
    source = '<svg>\n<text>keep \t\nspaces </text>\n<path id="a" d="M 0 0 \t\nL 1 1 \n"/>\n</svg>'
    expected = source.replace('M 0 0 \t\n', 'M 0 0\n').replace('L 1 1 \n', 'L 1 1\n')
    for split in range(len(source) + 1):
      with self.subTest(split=split):
        output = io.StringIO()
        writer = SvgWriter(output)
        self.assertEqual(writer.write(source[:split]), split)
        writer.flush()
        self.assertEqual(writer.write(source[split:]), len(source) - split)
        writer.finish()
        self.assertEqual(output.getvalue(), expected)
        self.assertFalse(output.closed)

  def test_streams_complete_lines(self):
    """Emit complete lines before finishing and retain a partial line."""
    output = io.StringIO()
    writer = SvgWriter(output)
    for character in '<path d="M 0 0 \nL 1':
      writer.write(character)
    self.assertEqual(output.getvalue(), '<path d="M 0 0\n')
    writer.flush()
    self.assertEqual(writer.pending, 'L 1')
    writer.write(' 1 \n"/>')
    writer.finish()
    self.assertEqual(output.getvalue(), '<path d="M 0 0\nL 1 1\n"/>')

  def test_closed_path_does_not_affect_text(self):
    """Retain whitespace following a path and in subsequent text lines."""
    source = '<path d="M 0 0"/> \n<text>hello \nworld </text> \n'
    output = io.StringIO()
    writer = SvgWriter(output)
    writer.write(source)
    writer.finish()
    self.assertEqual(output.getvalue(), source)


if __name__ == "__main__":
  unittest.main()
