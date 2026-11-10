# Matched preload performance

Each row compares revisions on one host/job. Lower latency is better.

| Machine / stage | Baseline ms | Migrated ms | Change ms | Baseline % | Change % |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux CI / shared worker | 1.3620 | 1.2270 | -0.1350 | 90.09% | -9.91% |
| Windows CI / shared worker | 3.9595 | 3.9540 | -0.0055 | 99.86% | -0.14% |
| macOS CI / shared worker | 4.4125 | 4.0810 | -0.3315 | 92.49% | -7.51% |
| Laptop / initial extraction | 5.0670 | 5.1705 | +0.1035 | 102.04% | +2.04% |
| Laptop / reviewed worker | 4.8295 | 4.8730 | +0.0435 | 100.90% | +0.90% |
| Laptop / path shadows | 5.5095 | 6.1340 | +0.6245 | 111.33% | +11.33% |

![Absolute and normalized medians](preload-comparison.png)

Full machine metadata, compiler flags, source digests and raw samples are in
[graph-evidence.json](graph-evidence.json). Missing historical hardware fields
remain unspecified. No timings are averaged across machines.
