#!/usr/bin/env python3
"""Issue 809 diagnostic: bypass ONLY the named test-pipe metadata open.

Patch an expendable checkout of git-for-windows/msys2-runtime. Never install
this experimental change into a user's regular Git for Windows environment.
The actual pipe open and Win32 connection path remain unchanged.
"""
from pathlib import Path
import argparse

ANCHOR = "      /* The EA given to NtCreateFile allows to get a handle to a symlink on\n"

HELPER_ANCHOR = "class symlink_info\n{\n"
FS_ANCHOR = "if (fs.inited ()|| fs.update (get_nt_native_path (), NULL))"

HELPER = r"""/* RWF #809: exact-match test endpoint; all other paths are untouched. */
static bool
git809_is_test_pipe_path (const char *candidate)
{
  const char *prefix = "\\\\.\\pipe\\git809-redirect-";
  const size_t prefix_len = strlen (prefix);
  if (!candidate || strlen (candidate) != prefix_len + 32
      || strncmp (candidate, prefix, prefix_len))
    return false;
  const char *p = candidate + prefix_len;
  for (int n = 0; n < 32; ++n)
    if (!((p[n] >= '0' && p[n] <= '9')
          || (p[n] >= 'a' && p[n] <= 'f')))
      return false;
  return true;
}

"""

FS_REPLACEMENT = r"""if (fs.inited ()
	      || (!((opt & PC_KEEP_HANDLE)
		    && git809_is_test_pipe_path (get_win32 ()))
		  && fs.update (get_nt_native_path (), NULL)))"""

PATCH = r"""      /*
       * RWF #809 isolated test: the test-pipe endpoint is a non-symlink,
       * non-directory, non-device.  Skip only the speculative metadata
       * NtCreateFile; the caller will still perform its normal data open.
       * This is not a general rule for Windows named pipes.
       *
       * Postconditions of the ordinary existing-file path consumed by
       * path_conv::check: no symlink/content, no device, valid attributes,
       * zero error, no appended suffix, no reparse flags, original mount
       * flags, original path length, and no owned metadata handle.
       */
      {
        if ((pc_flags () & PC_KEEP_HANDLE)
            && git809_is_test_pipe_path (suffix.path))
          {
            /* reset() and suffix.has() ran above; suffix.next() is
             * already active.  Keep mount_flags and path_len intact. */
            if (h || conv_hdl.handle ())
              api_fatal ("git809 test-pipe bypass reached with open metadata handle");
            fileattr (FILE_ATTRIBUTE_NORMAL);
            error (0);
            issymlink (false);
            dev (FH_NADA);
            path_flags (0);
            ext_tacked_on (false);
            clear_content ();
            assert (!issymlink () && !isdevice ()
                    && error () == 0
                    && fileattr () == FILE_ATTRIBUTE_NORMAL
                    && !ext_tacked_on () && path_flags () == 0);
            return 0;
          }
      }
"""

def main():
  p = argparse.ArgumentParser()
  p.add_argument("path", type=Path)
  args = p.parse_args()
  target = args.path
  source = target.read_text(encoding="utf-8")
  assert source.count(ANCHOR) == 1, "unexpected MSYS2 symlink-info source"
  assert source.count(HELPER_ANCHOR) == 1, "unexpected source class declaration"
  assert source.count(FS_ANCHOR) == 1, "unexpected filesystem fallback"
  import re
  # The SDK packaging patches and the upstream source use different layout.
  # Match control flow and declaration invariants, not indentation.
  function_start = source.index("symlink_info::check (")
  function_body = source[function_start:source.index('\n/* "path" is the path', function_start)]
  before_metadata = function_body[:function_body.index(ANCHOR)]
  assert re.search(r"\breset\s*\(\s*\)\s*;", before_metadata), "reset() must precede special case"
  assert re.search(r"\bh\s*=\s*NULL\s*;", before_metadata), "metadata handle must be initialized"
  assert re.search(r"\bwhile\s*\(\s*suffix\.next\s*\(\s*\)\s*\)", before_metadata), "suffix candidate loop changed"
  assert re.search(r"\bif\s*\(\s*h\s*\)\s*\{\s*NtClose\s*\(\s*h\s*\)", before_metadata), "prior metadata handle cleanup changed"
  assert "path_len (ext_here - path);" in before_metadata, "path length prerequisite missing"
  assert "error (0);" in before_metadata, "error state prerequisite missing"
  assert "RWF #809 isolated test" not in source, "already patched"
  patched = source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
  patched = patched.replace(ANCHOR, PATCH + ANCHOR, 1)
  patched = patched.replace(FS_ANCHOR, FS_REPLACEMENT, 1)
  target.write_text(patched, encoding="utf-8")
  print("GIT809_METADATA_PATCH_APPLIED", target)

if __name__ == "__main__":
  main()
