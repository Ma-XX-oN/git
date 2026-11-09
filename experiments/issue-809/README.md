# RWF #809 Windows native diagnostic: debug-symbol contract

This file documents the **experimental** Detours exit-path diagnosis on branch
`investigation-809-exit-detours`. It does not change the upstream Git build
policy or authorize changing production IPC behavior.

## Build configuration

Git's Makefile has `CFLAGS = -g -O2 -Wall` by default. `-g` generates
debugging information but `-O2` optimizes away or rearranges source-level
states. For native Windows fault and stack investigations, explicitly build
the same-SHA Git executable using the supported Make variable override:

```sh
make -j4 NO_RUST=YesPlease NO_CURL=YesPlease NO_GETTEXT=YesPlease \
  CFLAGS='-g -O0 -fno-omit-frame-pointer -Wall' git.exe
```

Do **not** assume that `-g` disables optimizations. Assert that the linked
`git.exe` contains `.debug_info` and `.debug_line` sections using
`objdump -h git.exe`. Preserve the exact source SHA and flags.

Detours instrumentation is built by MSVC separately: compile with `/Zi /Od`
and link with `/DEBUG:FULL`; assert that the corresponding DLL/EXE PDBs
exist and archive them alongside diagnostic logs. The pinned Microsoft Detours
4.0.1 `src/Makefile` itself uses `/Zi /Od` and `/Fd$(LIBD)\\detours.pdb`;
`system.mak` resolves `LIBD` to `lib.X64` for our x64 build. Preserve
`lib.X64/detours.pdb` as well. Do not confuse MSVC PDBs with Git's
MinGW/GCC DWARF symbols. The Git SDK Bash login profile resets `PATH`:
run the Python PE/COFF debug-section verification in a separate PowerShell
step where `actions/setup-python` is available.

## Reuse the verified prior RWF #809 symbol workbench

Before changing symbol-build logic, inspect these existing experiment branches:

- `experiment-809-symbolized-detours-runtime`:
  `.github/workflows/issue-809-symbolized-runtime.yml` uses the full
  Git-for-Windows SDK and a matching symbol-bearing MSYS2 runtime.
- `experiment-809-msys2-symbols-o0`:
  `.github/workflows/issue-809-msys2-symbolized-detours.yml` verifies
  the actual raw DLL's DWARF sections before symbolization.
- `experiment-809-detours-handle-trace`:
  `.github/workflows/issue-809-detours-handle-trace.yml` retains and
  verifies matching runtime symbols before `addr2line`.

**Runner-shell rule:** Git SDK's minimal Bash PATH did not expose
`python` or `objdump` in native runs 42556061777 and 42555846182,
respectively. Do not invoke these programs from that Bash step without
verifying availability. Compile Git under Git SDK Bash and run
`check_pe_debug_sections.py` using the installed native Python from a
PowerShell step. Preserve the exact same-SHA, DWARF-bearing `git.exe`
in the uploaded diagnostic artifact, as well as the Detours PDB files.
Fail if any required symbol material is missing. Do not treat build
success or `-g` being present as proof that symbols survived linking.

## Crash investigation and symbolization

1. Record the exact source SHA, compiler/linker flags, architecture, and
   binary identity for the runner producing the crash. Only compare evidence
   from matching builds and fixtures.
2. Capture the **faulting** instruction pointer and context, not the stack
   of the exception logging callback. Record module name, module base,
   relative address (RVA), and the bounded unwind frames. Cap recurring
   first-chance exception logging; do not change the exception disposition.
3. For Git's GCC-built PE executable, inspect the exact `git.exe` with
   `addr2line -e git.exe -f -C`, using the address representation that its
   PE image base and symbol table expect. Cross-check section addresses
   with `objdump -h`; do not submit an ASLR-loaded absolute address
   unchanged as though it were an on-disk symbol address.
4. For the MSVC-built tracer/launcher, resolve addresses against the exact
   matching PDB. For Windows system DLLs, obtain matching public Microsoft
   symbols; our own PDBs cannot symbolize OS DLL internals.
5. An assertion that a legacy configuration *must* crash is invalid when
   reproduction is intermittent. Record successful and crashed attempts
   separately. A normal-run PASS never proves that a different, crashing
   configuration is fixed.
6. Verify the exact source SHA on the Windows-native CI runner after every
   diagnostic change. A committed patch, successful compilation, or captured
   address without source attribution is not source-level crash acceptance.

## RWF #809 incident record

The first Detours runs omitted explicit `/Zi`, `/DEBUG`, preserved PDBs,
and an enforced unoptimized Git build. An access violation `0xC0000005`
was then captured as anonymous frame offsets. In run
`42555175051` at SHA `4a5e146002cede7e7c67aaf95e8f8498aedba559`,
five of twelve legacy-MSVCRT attempts crashed, while the normal UCRT
worker passed. This is historical evidence, not acceptance of any newer
debug-symbol build.

**Rule for future diagnostics:** set explicit debug-symbol and
no-optimization build flags, verify their output, and retain symbol files
*before* attempting stack-based root-cause analysis.
