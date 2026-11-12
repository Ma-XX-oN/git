# Native manager implementation checklist

The generic C++17 `preload-manager` replaces the Python session manager.
The worker and shared transport remain C. The maintained wire contract is
[protocol.adoc](protocol.adoc).

- [x] Create [worker extraction and full audit ticket #13](https://github.com/Ma-XX-oN/git/issues/13).
- [x] Move transport into shared source/header directories and expose C/C++ headers.
- [ ] Verify shared transport behavior with rebuilt worker tests.
- [ ] Build and validate shared pools, native processes, and platform request servers.
- [ ] Preserve deadlines, environment inheritance, pool policy, status, forwarding, ownership and cleanup.
- [ ] Wire Make/Meson and Bash/PowerShell to the native executable.
- [ ] Validate failures, concurrency, crashes, context/output, argument limits and 6000 serial calls.
- [ ] Implement/run matched Python/C++ manager and real-client comparisons.
- [ ] Record all latency/throughput/resource workloads, raw evidence, intervals and charts.
- [ ] Validate Windows, Linux and macOS with both build systems.
- [ ] Remove Python runtime after parity, sweep maintained references, and update usage docs.

Baseline revision: `2c271ea5357d7993d3f82affdb348aa2ad1f1ddc`.
Compare both managers with the same worker binary and protocol; do not infer
performance gains from measurements made on different machines or jobs.
