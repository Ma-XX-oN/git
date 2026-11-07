#!/usr/bin/env python3
"""Issue 809 diagnostic: bypass ONLY the named test-pipe metadata open.

Patch an expendable checkout of git-for-windows/msys2-runtime. Never install
this experimental change into a user's regular Git for Windows environment.
The actual pipe open and Win32 connection path remain unchanged.
"""
from pathlib import Path
import argparse

ANCHOR = "      /* The EA given to NtCreateFile allows to get a handle to a symlink on\n"

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
        const char *prefix = "\\\\.\\pipe\\git809-redirect-";
        const size_t prefix_len = strlen (prefix);
        const char *candidate = suffix.path;
        bool test_pipe = (pc_flags () & PC_KEEP_HANDLE)
                         && strlen (candidate) == prefix_len + 32
                         && strncmp (candidate, prefix, prefix_len) == 0;
        if (test_pipe)
          {
            const char *p = candidate + prefix_len;
            for (int n = 0; n < 32; ++n)
              if (!((p[n] >= '0' && p[n] <= '9')
                    || (p[n] >= 'a' && p[n] <= 'f')))
                test_pipe = false;
          }
        if (test_pipe)
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
  assert source.count("      reset ();") >= 1, "missing reset prerequisite"
  assert source.count("      if (h)\n\t{\n\t  NtClose (h);") >= 1, "missing loop cleanup prerequisite"
  assert "      while (suffix.next ())" in source, "suffix initialization changed"
  assert source.index("      while (suffix.next ())") < source.index(ANCHOR), "insertion outside candidate loop"
  assert "RWF #809 isolated test" not in source, "already patched"
  patched = source.replace(ANCHOR, PATCH + ANCHOR, 1)
  target.write_text(patched, encoding="utf-8")
  print("GIT809_METADATA_PATCH_APPLIED", target)

if __name__ == "__main__":
  main()
