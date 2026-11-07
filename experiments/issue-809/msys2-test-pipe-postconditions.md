# RWF #809 — isolated MSYS2 test-pipe metadata exception

Status: **EXPERIMENTAL, not production accepted** (2026-11-07).

## Origin and evidence

- Windows runner reproduction (GitHub Actions run 42378130994):
  the first native named-pipe instance is opened and closed without data;
  Git Bash's second connection carries the actual redirection data.
  One-instance redirection fails for both read and write.
- The installed Git-for-Windows MSYS2 3.6.10-4 runtime uses
  `winsup/cygwin/syscalls.cc:open()` → `build_fh_name()` →
  `path_conv::check()` → `symlink_info::check()`.
  The latter opens the pathname with `NtCreateFile` to inspect attributes
  before `fhandler_base::open()` performs the normal data open.
- Source examined: git-for-windows/msys2-runtime
  `da84778cbc92590c1996c02c68c5a3d3c49e00af`.
- Implementation source: `patch_msys2_test_pipe_attributes.py`.
  Packaging recipe: MSYS2-packages at
  `af487d629edc364cac6388e3e072b70d652236a3` (3.6.10-4).

## Exceptional path / caller postconditions

The exception is allowed **only** for the exact
`\\.\pipe\git809-redirect-` prefix with precisely 32 lowercase
hexadecimal characters, after the original function has performed
`reset()`, `suffix.has()`, `path_len()`, and `suffix.next()`.
All other names must execute the original function unchanged.

| Field/resource | Required after returning zero | Basis |
|---|---|---|
| `_fileattr` | `FILE_ATTRIBUTE_NORMAL` | Existing regular-file path recognized |
| `_issymlink` | false | No symlink expansion |
| `_dev` | `FH_NADA` | Not a device |
| `_error` | 0 | Existing path, no error |
| `_path_flags` | 0 | No socket, symlink or reparse point |
| `_ext_tacked_on` | false | No synthetic suffix |
| `contents` | empty | No symlink content |
| `_mount_flags` | preserve | Input flags from caller |
| `_path_len` | preserve | Determined by existing suffix scan |
| `ext_here` | preserve | Set by `suffix.has()` |
| `h` | NULL, never acquired | Do not retain or close nonexisting handle |
| `conv_hdl` | no newly acquired handle | `path_conv::check()` initialization closes earlier one |
| `fs` | retains cleared/unknown state for this test endpoint | Caller suppresses only the name-matched `fs_info::update` lookup, which otherwise performs another `NtOpenFile` on the test pipe |

`mode()` is not required because `isdevice()` is false; the caller
reads mode only for device paths. The real data open remains unchanged. Importantly, the caller's
`fs_info::update(get_nt_native_path(), NULL)` would otherwise open
the same pipe pathname again (see `winsup/cygwin/mount.cc`, `fs_info::update`).
The exception therefore guards this **second** metadata open as well.
It uses the existing, explicitly supported "filesystem lookup unavailable"
path; it does not fabricate volume flags. The original filesystem lookup
remains in force for every nonmatching pathname.

## Native acceptance

The isolated CI workflow `issue-809-msys2-metadata-bypass.yml` must:

1. Build the historical runtime on a disposable Windows runner.
   Verify the source tag and record SHA and DLL hash.
2. Demonstrate that the original Git Bash runtime fails with one pipe
   instance for both read and write.
3. Demonstrate that the patched runtime reads and writes correct bytes
   using one instance for exact test names.
4. Verify that nonmatching prefixes and malformed hexadecimal names
   retain their original behavior (one instance fails, two succeed).
5. Verify that normal-file redirection remains correct.
6. Load the patched runtime only beside an isolated copy of Bash; leave the SDK runtime untouched throughout, including on failure.
7. Verify the source patch's postconditions as release-active checks and preserve the cleared filesystem-info fallback for this synthetic endpoint.

A successful patch application **is not** native acceptance. Do not
modify a production MSYS2 or Git-for-Windows installation.

## Native transport control

Actions run `42380922356` verifies direct Win32 clients complete read and write with one named-pipe instance, whereas the stock Git Bash redirection uses two. The first Git Bash connection closes without transferring data; its second connection transfers the payload. The same test confirms the installed Git Bash reports `dynamic loading not available` for the loadable-builtin attempt. This does not establish whether the isolated pathname override succeeds.

The historical MSYS2 build requires both `x86_64-w64-mingw32-gcc` and `i686-w64-mingw32-gcc`, in addition to the MSYS GCC. The i686 package is `mingw-w64-cross-mingw32-gcc` (tools under `/opt/bin`); its absence caused Actions run `42381679077` to fail during configuration, before compiling the override.
