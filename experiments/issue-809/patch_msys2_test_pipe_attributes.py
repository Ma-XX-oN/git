#!/usr/bin/env python3
"""Issue 809 diagnostic: bypass ONLY the named test-pipe metadata open.

Patch an expendable checkout of git-for-windows/msys2-runtime. Never install
this experimental change into a user's regular Git for Windows environment.
The actual pipe open and Win32 connection path remain unchanged.
"""
from pathlib import Path
import argparse

ANCHOR = """      /* The EA given to NtCreateFile allows to get a handle to a symlink on
         an NFS share, rather than getting a handle to the target of the
         symlink (which would spoil the task of this method quite a bit).
         Fortunately it's ignored on most other file systems so we don't have
         to special case NFS too much. */"""

PATCH = r"""      /*
       * RWF #809 temporary experiment, deliberately restricted to the
       * \\.\\pipe\\git809-redirect-<32 lowercase hex digits> namespace.
       * The only aim is to test whether the normal preliminary NtCreateFile
       * attribute query consumes one native named-pipe connection.  Do not
       * ship this exception as a general MSYS2 policy.
       */
      {
        const char *prefix = "\\\\.\\pipe\\git809-redirect-";
        const size_t prefix_len = strlen (prefix);
        const char *candidate = suffix.path;
        bool test_pipe = strlen (candidate) == prefix_len + 32
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
            fileattr (FILE_ATTRIBUTE_NORMAL);
            goto file_not_symlink;
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
  assert "RWF #809 temporary experiment" not in source, "already patched"
  patched = source.replace(ANCHOR, PATCH + ANCHOR, 1)
  target.write_text(patched, encoding="utf-8")
  print("GIT809_METADATA_PATCH_APPLIED", target)

if __name__ == "__main__":
  main()
