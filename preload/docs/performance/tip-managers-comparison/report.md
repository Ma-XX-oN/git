# Unmodified Git vs the tip's Python and C++ managers

Each group is measured within one job on one machine. Unmodified Git is the
upstream revision this work branches from, built with identical flags and
launched directly. Both preloaded variants are the same tip build (worker and
clients), with the C++ manager or the Python manager. PowerShell groups also
time the generated git function. Percentages use the same client's
unmodified reference. Superscript and subscript values are the distances to
the bounds of the 95% bootstrap confidence interval of the median.

| Group | Unmodified ms | Python ms | C++ ms | Python fn ms | C++ fn ms | Python % | C++ % | Python fn % | C++ fn % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Windows CI, Xeon 8370C / 4 CPUs, Bash | 22.14<sup>+0.15</sup><sub>-0.17</sub> | 4.126<sup>+0.084</sup><sub>-0.332</sub> | 3.88<sup>+0.14</sup><sub>-0.16</sub> | — | — | 18.63%<sup>+0.41</sup><sub>-1.48</sub> | 17.53%<sup>+0.66</sup><sub>-0.72</sub> | — | — |
| Windows CI, Xeon 8370C / 4 CPUs, PowerShell | 11.66<sup>+0.23</sup><sub>-0.17</sub> | 3.641<sup>+0.065</sup><sub>-0.036</sub> | 3.465<sup>+0.044</sup><sub>-0.069</sub> | 3.821<sup>+0.051</sup><sub>-0.047</sub> | 3.791<sup>+0.073</sup><sub>-0.042</sub> | 31.21%<sup>+0.64</sup><sub>-0.67</sub> | 29.70%<sup>+0.57</sup><sub>-0.75</sub> | 32.75%<sup>+0.63</sup><sub>-0.75</sub> | 32.50%<sup>+0.68</sup><sub>-0.67</sub> |
| Linux CI, EPYC 7763 / 4 CPUs | 1.3865<sup>+0.0135</sup><sub>-0.0095</sub> | 1.128<sup>+0.017</sup><sub>-0.010</sub> | 1.0135<sup>+0.0115</sup><sub>-0.0075</sub> | — | — | 81.4%<sup>+1.3</sup><sub>-1.1</sub> | 73.10%<sup>+0.89</sup><sub>-0.86</sub> | — | — |
| macOS CI, M1 Virtual / 3 CPUs | 12.68<sup>+0.54</sup><sub>-0.45</sub> | 3.76<sup>+0.19</sup><sub>-0.36</sub> | 3.13<sup>+0.28</sup><sub>-0.24</sub> | — | — | 29.6%<sup>+2.0</sup><sub>-3.3</sub> | 24.7%<sup>+2.1</sup><sub>-2.1</sub> | — | — |
| Laptop, Ryzen 7 5700U / 16 CPUs, Bash | 57.20<sup>+0.36</sup><sub>-0.33</sub> | 4.024<sup>+0.085</sup><sub>-0.060</sub> | 3.870<sup>+0.063</sup><sub>-0.084</sub> | — | — | 7.04%<sup>+0.16</sup><sub>-0.12</sub> | 6.77%<sup>+0.12</sup><sub>-0.17</sub> | — | — |
| Laptop, Ryzen 7 5700U / 16 CPUs, PowerShell | 28.34<sup>+0.22</sup><sub>-0.22</sub> | 4.65<sup>+0.11</sup><sub>-0.10</sub> | 4.710<sup>+0.070</sup><sub>-0.089</sub> | 5.19<sup>+0.13</sup><sub>-0.10</sub> | 4.969<sup>+0.112</sup><sub>-0.098</sub> | 16.40%<sup>+0.41</sup><sub>-0.39</sub> | 16.62%<sup>+0.28</sup><sub>-0.33</sub> | 18.33%<sup>+0.48</sup><sub>-0.37</sub> | 17.53%<sup>+0.40</sup><sub>-0.35</sub> |

![Medians with 95% confidence intervals](preload-tip-managers.png)

Machine metadata, compiler flags, revisions, binary digests and raw samples
are in [graph-evidence.json](graph-evidence.json). No timings are pooled
across machines.
