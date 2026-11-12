# Library validation: Bash and PowerShell

Tested implementation: `83c911b2b4ee66cb4a1ffc7356bba4c1db9d1362`.
[CI run 43309409610](https://github.com/Ma-XX-oN/git/actions/runs/43309409610)
builds the worker with Make and Meson on Linux, macOS and Windows.

## Acceptance

All three native jobs passed. Make-built workers passed lifecycle, transport,
manager dispatch, owner death, worker crash, argument boundaries, environment,
selected existing command regressions, output handling and 6,000-command Bash
stress checks. Meson-built workers passed lifecycle, selected command regressions
and the integrated `t5790-preload` suite. Linux and macOS also passed builds
without pthread support and the expected rejection of worker mode in those builds.

Windows PowerShell 7.6.9 passed all 23 correctness and session-lifecycle checks
against both Make and Meson binaries, plus a separate 6,000-command stress run.
Checks include capture/discard modes, Unicode, both output streams, exit status,
65,535 parameters, overflow rejection, killed-worker status/recovery, duplicate
start, re-sourcing an active session, stop and failed-start cleanup.
The full ordinary-Git suite was excluded as required for development validation.

## Matched production comparison

Medians in milliseconds for repeated `git --version`, with environment inherited
from manager startup, three workers and three ABBA comparison blocks. Each row
compares executables built in the same job with matching compiler flags.

| Platform | Upstream direct | Production preload | Library Bash | Library / production | Library / direct |
|---|---:|---:|---:|---:|---:|
| Linux | 1.168 | 0.835 | 0.912 | 109.2% | 78.1% |
| macOS | 7.810 | 2.016 | 2.213 | 109.8% | 28.3% |
| Windows | 23.3435 | 4.4155 | 4.289 | 97.1% | 18.4% |

The preserved production preload baseline is
`1af6a02b64d65554f4974a5ca08443529a71eb4b`; upstream direct Git is
`750f5de7dfe9a4062f95088212b063f189d50e91`. These comparisons cover the
whole migration, including protocol and client changes. They do not isolate
the directory restructure or the latest commit. The library is faster than
direct execution but about 9-10% slower than production preload on these Unix
runners. No profiling in this run identifies the cause of that difference.
Windows improved by 2.9% against production preload in this comparison.

The separate READY-dispatch comparison uses matching worker protocols:

| Platform | Workload | Before READY ms | Library ms | Library / before |
|---|---|---:|---:|---:|
| Linux | shell | 0.914 | 0.916 | 100.2% |
| Linux | command substitution | 1.612 | 1.615 | 100.2% |
| macOS | shell | 2.3375 | 2.221 | 95.0% |
| macOS | command substitution | 3.876 | 3.5925 | 92.7% |
| Windows | shell | 4.3995 | 4.269 | 97.0% |
| Windows | command substitution | 15.185 | 14.9845 | 98.7% |

## Windows PowerShell versus Bash

Both clients used the same Make-built worker executable. PowerShell drained
stdout to `Stream.Null` and Bash redirected stdout to `/dev/null`. Two ABBA
blocks measured 800 calls per client, with 20 warm-up calls per process and one
direct call between measured calls. Manager startup and the one-time C# connector
compilation are excluded.

| Client | Median ms | p90 ms | Relative to Bash |
|---|---:|---:|---:|
| Bash | 4.0505 | 4.360 | 100.0% |
| PowerShell | 3.16785 | 3.7818 | 78.2% |

PowerShell was 21.8% faster in this run. Its direct-execution reference measured
11.55885 ms, giving preloaded PowerShell 27.4% of direct latency. That reference
uses PowerShell to invoke the candidate executable; the production comparison
above invokes unmodified upstream Git through Bash. Those two direct figures
are distinct measurements and should not be substituted for each other.

This benchmark covers serial `--version` calls with discarded output. It does
not establish stdin, pipeline/redirection equivalence, startup performance or
the cost of capturing and decoding application output.

## Machines and raw evidence

Linux runner: AMD EPYC 9V74, four assigned logical CPUs, x86_64,
Linux 7.0.0-1019-azure/glibc 2.43, Python 3.13.18, Bash 5.3.9.
macOS runner: Apple M1 virtual machine, three assigned logical CPUs, arm64,
macOS 26.7.2, Python 3.13.17, Bash 5.3.20. Hardware differs between rows;
cross-platform timing differences do not measure an operating-system effect.
Windows runner: AMD EPYC 7763, four assigned logical CPUs, AMD64,
Windows Server 2025 build 26100, Python 3.13.17, Bash 5.3.20 (Cygwin),
PowerShell 7.6.9.

Exact CI archives, including machine metadata, executable hashes, build flags,
per-round timing samples, manager logs, Make acceptance results and Meson logs:

- [Linux](evidence/preload-ci-83c911b2b4-linux.zip)
- [macOS](evidence/preload-ci-83c911b2b4-macos.zip)
- [Windows](evidence/preload-ci-83c911b2b4-windows.zip), including
  `preload-powershell-comparison/comparison.json` and individual run samples.
