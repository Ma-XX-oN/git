#!/usr/bin/env python3
"""SPECULATIVE upstream-oriented named-pipe metadata query experiment.

Targets the historical MSYS2/Cygwin 3.6.10-4 path.cc only; independently
verify its preconditions and patch actual source, never the installed runtime.

The optimization applies to the Windows native named-pipe object namespace,
not to an application/test-specific endpoint. No attributes are fabricated:
the kernel must return them through NtQueryAttributesFile, or original code
remains in force. No handle is acquired by this added user-mode query.
"""
from pathlib import Path
import argparse

CLASS_ANCHOR = "class symlink_info\n{\n"
OPEN_ANCHOR = (
    "      /* The EA given to NtCreateFile allows to get a handle to a symlink on\n"
)
FS_ANCHOR = "if (fs.inited ()|| fs.update (get_nt_native_path (), NULL))"

HELPER = r"""/* Diagnostic uses the native stderr handle, without file path resolution. */
static void
git809_path_event (const char *event)
{
  HANDLE err = GetStdHandle (STD_ERROR_HANDLE);
  DWORD written;
  if (err && err != INVALID_HANDLE_VALUE)
    WriteFile (err, event, (DWORD) strlen (event), &written, NULL);
}

/* Native pipe objects have no drive volume to interrogate. This tests
   the NT object namespace, never a filename or application prefix. */
static bool
is_native_named_pipe_path (const UNICODE_STRING *path)
{
  static const WCHAR dos_pipe[] = L"\\??\\pipe\\";
  static const WCHAR nt_pipe[] = L"\\Device\\NamedPipe\\";
  const WCHAR *prefix = NULL;
  size_t count = 0;

  if (!path || !path->Buffer)
    return false;
  const size_t n = path->Length / sizeof (WCHAR);
  const size_t dos_count = (sizeof (dos_pipe) / sizeof (WCHAR)) - 1;
  const size_t nt_count = (sizeof (nt_pipe) / sizeof (WCHAR)) - 1;
  for (int candidate = 0; candidate < 2; ++candidate)
    {
      prefix = candidate ? nt_pipe : dos_pipe;
      count = candidate ? nt_count : dos_count;
      if (n <= count)
        continue;
      size_t i = 0;
      for (; i < count; ++i)
        if (RtlUpcaseUnicodeChar (path->Buffer[i])
            != RtlUpcaseUnicodeChar (prefix[i]))
          break;
      if (i == count)
        return true;
    }
  return false;
}

"""

PROBE = r"""      /*
       * Speculative: native named pipes are not files on a mounted volume.
       * An attribute-only NtCreateFile consumes a real server pipe instance.
       * Prefer the non-handle-returning NT metadata API when possible.
       *
       * Do not synthesize attributes or discard errors: if the query fails,
       * or special-file attributes require handle-based processing, fall
       * through to the original metadata open and symlink/reparse logic.
       */
      if (is_native_named_pipe_path (&upath))
        {
          git809_path_event ("GIT809_PATH QUERY_ENTER\n");
          FILE_BASIC_INFORMATION basic;
          NTSTATUS query_status = NtQueryAttributesFile (&attr, &basic);
          if (!NT_SUCCESS (query_status))
            git809_path_event ("GIT809_PATH QUERY_FAILED\n");
          else if (basic.FileAttributes & (FILE_ATTRIBUTE_DIRECTORY |
                    FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_SYSTEM))
            git809_path_event ("GIT809_PATH QUERY_REJECT_ATTRIBUTES\n");
          else
            git809_path_event ("GIT809_PATH QUERY_ACCEPTED\n");
          if (NT_SUCCESS (query_status)
              && !(basic.FileAttributes
                   & (FILE_ATTRIBUTE_DIRECTORY
                      | FILE_ATTRIBUTE_REPARSE_POINT
                      | FILE_ATTRIBUTE_SYSTEM)))
            {
              fileattr (basic.FileAttributes);
              ext_tacked_on (!!*ext_here);
              git809_path_event ("GIT809_PATH BYPASS_GOTO\n");
              debug_printf ("named pipe metadata without open: %S", &upath);
              goto file_not_symlink;
            }
          git809_path_event ("GIT809_PATH QUERY_FALLTHROUGH\n");
        }
"""

FS_REPLACEMENT = r"""if (fs.inited ()
\t      || (is_native_named_pipe_path (get_nt_native_path ())
\t\t  ? (git809_path_event ("GIT809_PATH FS_UPDATE_SKIPPED\n"), false)
\t\t  : fs.update (get_nt_native_path (), NULL)))"""

def main() -> None:
  p = argparse.ArgumentParser()
  p.add_argument("path", type=Path)
  args = p.parse_args()
  source = args.path.read_text(encoding="utf-8")
  for anchor in [CLASS_ANCHOR, OPEN_ANCHOR, FS_ANCHOR]:
    if source.count(anchor) != 1:
      raise ValueError(f"unexpected source layout: {anchor!r}")
  if "named pipe metadata without open" in source:
    raise ValueError("already patched")
  function = source[source.index("symlink_info::check ("):]
  if not (
    function.index("reset ();")
    < function.index("h = NULL;")
    < function.index("while (suffix.next ())")
    < function.index(OPEN_ANCHOR)
  ):
    raise ValueError("expected metadata state initialization changed")
  label = "file_not_symlink:"
  assert source.count(label) == 1, "expected exact bypass target label"
  result = source.replace(CLASS_ANCHOR, HELPER + CLASS_ANCHOR, 1)
  metadata_event = (
    '      if (is_native_named_pipe_path (&upath))\n'
    '        git809_path_event ("GIT809_PATH METADATA_OPEN_REACHED\\n");\n'
  )
  result = result.replace(OPEN_ANCHOR, PROBE + metadata_event + OPEN_ANCHOR, 1)
  result = result.replace(label, label +
    '\n      if (is_native_named_pipe_path (&upath))\n'
    '        git809_path_event ("GIT809_PATH FILE_NOT_SYMLINK\\n");', 1)
  result = result.replace(FS_ANCHOR, FS_REPLACEMENT, 1)
  args.path.write_text(result, encoding="utf-8")
  print("GIT809_UPSTREAM_SPECULATIVE_QUERY_PATCH", args.path)

if __name__ == "__main__":
  main()
