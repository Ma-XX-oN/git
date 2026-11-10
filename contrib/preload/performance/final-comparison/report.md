# Matched preload performance

Each row compares revisions on one host/job. Lower latency is better.

| Machine / stage | Baseline ms | Migrated ms | Change ms | Baseline % | Change % |
| --- | ---: | ---: | ---: | ---: | ---: |
| Windows CI / final cleanup | 6.0075 | 4.1750 | -1.8325 | 69.50% | -30.50% |
| Linux CI / final cleanup | 1.8050 | 1.0500 | -0.7550 | 58.17% | -41.83% |
| macOS CI / final cleanup | 4.0470 | 2.5290 | -1.5180 | 62.49% | -37.51% |
| Laptop / final cleanup | 5.0000 | 4.1305 | -0.8695 | 82.61% | -17.39% |

![Absolute and normalized medians](preload-comparison.png)

Full machine metadata, compiler flags, source digests and raw samples are in
[graph-evidence.json](graph-evidence.json). Missing historical hardware fields
remain unspecified. No timings are averaged across machines.

Windows CI / final cleanup: git --version; environment unchanged after manager startup. Baseline: complete environment snapshot per command; candidate: environment inherited from manager startup. This row measures the stated contract as well as implementation changes.

Linux CI / final cleanup: git --version; environment unchanged after manager startup. Baseline: complete environment snapshot per command; candidate: environment inherited from manager startup. This row measures the stated contract as well as implementation changes.

macOS CI / final cleanup: git --version; environment unchanged after manager startup. Baseline: complete environment snapshot per command; candidate: environment inherited from manager startup. This row measures the stated contract as well as implementation changes.

Laptop / final cleanup: git --version; environment unchanged after manager startup. Baseline: complete environment snapshot per command; candidate: environment inherited from manager startup. This row measures the stated contract as well as implementation changes.
