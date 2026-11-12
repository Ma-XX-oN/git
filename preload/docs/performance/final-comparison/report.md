# Migrated preload vs unmodified Git

Each row is measured within one job on one machine. Unmodified Git is the
upstream revision this work branches from, built with identical flags and
launched directly. Each direct call is interleaved with a preloaded call.
Superscript and subscript values are the distances to the upper and lower
bounds of the 95% bootstrap confidence interval of the median.

| Machine | Unmodified Git ms (95% CI) | Migrated preloaded ms (95% CI) | % of unmodified (95% CI) | Change % |
| --- | ---: | ---: | ---: | ---: |
| Windows CI, EPYC 9V74 / 4 CPUs | 16.94<sup>+0.29</sup><sub>-0.41</sub> | 2.893<sup>+0.014</sup><sub>-0.018</sub> | 17.08%<sup>+0.44</sup><sub>-0.30</sub> | -82.92% |
| Linux CI, EPYC 7763 / 4 CPUs | 1.421<sup>+0.017</sup><sub>-0.028</sub> | 1.0790<sup>+0.0050</sup><sub>-0.0070</sub> | 75.9%<sup>+1.6</sup><sub>-1.0</sub> | -24.1% |
| macOS CI, M1 Virtual / 3 CPUs | 7.13<sup>+0.21</sup><sub>-0.37</sub> | 2.381<sup>+0.093</sup><sub>-0.064</sub> | 33.4%<sup>+2.1</sup><sub>-1.2</sub> | -66.6% |
| Laptop, Ryzen 7 5700U / 16 CPUs | 55.20<sup>+1.03</sup><sub>-0.36</sub> | 4.181<sup>+0.049</sup><sub>-0.053</sub> | 7.575%<sup>+0.097</sup><sub>-0.175</sub> | -92.425% |

![Medians with 95% confidence intervals](preload-comparison.png)

Machine metadata, compiler flags, revisions, binary digests and raw samples
are in [graph-evidence.json](graph-evidence.json). No timings are pooled
across machines.
