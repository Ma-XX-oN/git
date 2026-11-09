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
exist and archive them alongside diagnostic logs. Do not confuse MSVC PDBs
with Git's MinGW/GCC DWARF symbols.

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
