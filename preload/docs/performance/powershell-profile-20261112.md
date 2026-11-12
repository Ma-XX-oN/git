# PowerShell client overhead

The principal verified overhead is the PowerShell function/pipeline path around
the four worker pipe opens. A temporary variant executing the same .NET pipe
constructor and `Connect(5000)` directly inside the existing channel loop reduced
the command median by 0.3042 ms (7.30%). The production prototype is unchanged;
the inline variant is a diagnostic, not a proposed duplication of shared code.

## Uninstrumented controls

All variants drain output to `Stream.Null` and return status only. ABCCBA order,
200 calls per run, two runs per variant, twenty warmups per run and one direct
Git call between measured calls. The worker binary and Python manager are shared.

| Variant | Median (ms) | Baseline (%) | p90 (ms) |
| --- | ---: | ---: | ---: |
| Original PowerShell discard client | 4.16765 | 100.00 | 5.6430 |
| Worker pipe opens in the existing loop | 3.86345 | 92.70 | 4.8065 |
| Synchronous PID/status read APIs | 4.25770 | 102.16 | 5.6236 |

The inline control removes four PowerShell function invocations, their argument
binding/scopes and the return of a pipe object through the PowerShell output
pipeline. These costs are measured collectively, not individually. Pipe options,
connection timeout, marker writes, protocol, manager and output drains are retained.

The synchronous-read control does not improve latency. It retains asynchronous
pipe handles and concurrent asynchronous output drains, and removes the short
reply APIs' per-read deadlines for diagnosis. It does not establish the cost of
all asynchronous I/O. The parent runner still bounds the whole process.

Both variants passed all 17 correctness checks, including captured and discarded
output, large stdout/stderr, Unicode/empty arguments and nonzero status. The
maximum 65535-parameter captured request and overflow rejection also passed.

## Stage profiles

Separate ABBA profiles recorded 400 calls per client. PowerShell uses temporary
copies with monotonic Stopwatch timestamps; Bash uses its existing stage stamps.
Instrumentation changes timings, so these locate costs rather than reproduce the
earlier uninstrumented end-to-end gap exactly. Stage medians do not add.

| Stage | Bash median (ms) | PowerShell median (ms) |
| --- | ---: | ---: |
| Prepare, connect manager, send request | 0.4750 | 0.6195 |
| Await PID reply | 0.0740 | 0.2418 |
| Decode PID, close request, connect worker channels | 0.7710 | 1.1313 |
| Start/drain output | 1.9180 | 1.6888 |
| Read status and cleanup | 0.1100 | 0.3108 |

Manager-side allocation and publication medians were 0.074/0.036 ms for the
PowerShell runs and 0.096/0.045 ms for Bash. The manager is not the source of
the PowerShell regression in these measurements. Output draining also does not
account for the slower PowerShell total. The uninstrumented inline control
confirms the channel-opening path as a useful optimization target.

A next implementation experiment can preserve one shared implementation while
moving that hot helper into a typed .NET method. That approach has not been timed.

The library integration replaces the function with the shared compiled .NET
method `Preload.Client.PipeConnector.Open`. The commands below operate on the client in the
checkout; the archived diagnostic sources preserve the exact earlier function
baseline and control variants used for the measurements in this report.

## Reproduction and evidence

Host: Windows 11 build 26200, AMD Ryzen 7 5700U, 16 logical CPUs, PowerShell
7.6.10, Python 3.13.1, Bash 5.3.9 (MSYS2/Cygwin runtime). Runtime DLLs for the
built Git must be on PATH. Run from the checkout root:

```text
python preload/perf/profile_powershell.py --git C:/path/to/git.exe --output C:/path/to/new-profile --count 200
python preload/perf/probe_powershell.py --git C:/path/to/git.exe --output C:/path/to/new-probes --count 200
```

- [Raw stage samples and source/binary hashes](evidence/powershell-stages-windows-20261112.json)
- [Raw uninstrumented controls, correctness results and hashes](evidence/powershell-probes-windows-20261112.json)
- [Temporary diagnostic PowerShell sources](evidence/powershell-profile-sources-20261112.zip)
- [Earlier matched discard comparison](evidence/powershell-discard-windows-20261112.json)
