# env-add/env-del cost, 2026-11-13

Windows laptop (Ryzen 7 5700U), Git built from `10219ce7e8` plus the
uncommitted `env-del`/`env-add` worker change, `DEVELOPER=1`,
`USE_PRELOAD_MANAGER=YesPlease`, C++ manager, three workers. The command was
`git var GIT_EDITOR` in every case.

## End-to-end ABBA (`end-to-end-abba.json`)

`preload/perf/env_delta_cost.py --rounds 20 --calls 50`: 1000 calls per arm
through a Python ctypes client. Arm A sent no environment fields; arm B sent
one `env-del` and four `env-add` entries. Median 12.321 versus 12.381 ms;
difference +60 us, bootstrap 95% interval -218 to +368 us. Call-to-call noise
is too large for this run to resolve microseconds.

## Worker-side apply time (`apply-trace-*.log`)

A temporary `GIT_TRACE_PRELOAD` line timed `preload_context_apply()` with
`getnanotime()` (not committed). Each line is one call: nanoseconds and the
context byte count, which identifies the arm.

`apply-trace-abba.log`: the same two arms as the end-to-end run
(`--rounds 4 --calls 200`, 811 calls per arm).

| Bytes | Fields | Median | p90 |
|---:|---|---:|---:|
| 33 | `cwd` only | 226.2 us | 379.0 us |
| 184 | `cwd`, 1 deletion, 4 assignments | 353.9 us | 559.9 us |

`apply-trace-scaling.log`: 150 interleaved calls per arm, run with
`env_delta_cost.call()` from an inline script.

| Bytes | Fields after `cwd` | Median | p90 |
|---:|---|---:|---:|
| 33 | none | 227.6 us | 389.2 us |
| 55 | `env-add`, 1 existing variable | 228.2 us | 376.4 us |
| 52 | `env-del`, 1 variable | 242.6 us | 448.2 us |
| 130 | `env-add`, 4 new variables | 355.0 us | 589.3 us |
| 400 | `env-add`, 16 new variables | 588.9 us | 1021.6 us |
| 8054 | `env-add`, 1 existing variable, 8000-character value | 366.4 us | 608.5 us |

Replacing an existing variable costs under 1 us; each new name costs about
22-32 us. The `cwd` change alone accounts for about 228 us and is present on
every call today. Linux and macOS were not measured.
