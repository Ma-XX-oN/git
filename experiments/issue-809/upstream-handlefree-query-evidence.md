# RWF 809: speculative handle-free metadata query for native named pipes

Status: **SPECULATIVE; no native acceptance yet.** Not an upstream submission.

## Problem and evidence

- A Git Bash redirection to a Win32 named pipe opens two native pipe connections; the first closes without transferring data. Direct Win32 open consumes one. Native control: GitHub Actions 42380922356.
- `symlink_info::check()` in MSYS2 3.6.10 opens the target via `NtCreateFile` before the data open, and `path_conv::check()` can make another volume-information open through `fs_info::update()`. Neither individual call has yet been conclusively attributed to the first connection.
- The earlier name-matched experiment, run 42384976461, failed compiling historical runtime startup code (`pinfo.cc` C++20 warning treated as error). It did not test the hypothesis and therefore does **not** justify cancelling this experiment.

## Candidate

Source base: git-for-windows/msys2-runtime commit `da84778cbc92590c1996c02c68c5a3d3c49e00af`. Patcher: `patch_msys2_upstream_handlefree_query.py`.

1. Recognize the Windows **native named-pipe namespace**, not any application/test name, after the existing path-to-NT conversion.
2. Query the kernel-provided basic attributes via `NtQueryAttributesFile`, an API already used elsewhere in `path.cc`.
3. If the query succeeds and attributes do not identify a reparse point, system file, or directory, treat the endpoint as an existing non-symlink and leave the actual communication open to `fhandler_base::open`.
4. Otherwise use the existing symlink, file, reparse, NFS, permissions, and cleanup logic.
5. Do not call volume-information lookup on the native named-pipe object namespace; it is not a disk filesystem.

The query has no user-mode handle output; the kernel may still internally open a pipe. This is why the native one-instance test is mandatory.

## Acceptance

- Exact candidate SHA builds with the historical MSYS2 package recipe.
- Existing MSYS2 `open02/open03`, `stat01/stat02/stat03`, `lstat02`, `readlink01/readlink02`, `symlink01/symlink02` run on the **newly built DLL** via the repository-prescribed test harness.
- Stock Bash fails to communicate with one named-pipe instance, reproducing the baseline.
- Patched Bash reads and writes exact payloads using **one** instance for three different valid native pipe names.
- Regular file read/write still succeeds, the installed SDK runtime DLL is never replaced, and the test DLL hash is recorded.
- Before upstream submission: extend native regression coverage to reparse points, shortcuts, NFS, unavailable endpoints, error semantics, and all affected path-conversion modes; run the full applicable testsuite.

If the earlier name-specific bypass eventually demonstrates that `symlink_info::check()` is *not* a failure contributor, stop this candidate and investigate the remaining metadata opens instead.

## CI

`.github/workflows/issue-809-upstream-handlefree.yml`, branch `experiment-809-upstream-handlefree-probe`. This experiment is isolated; do not merge it into Git or MSYS2 merely because compilation or a focused test succeeds.
