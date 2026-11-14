using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.Windows.EventTracing;
using Microsoft.Windows.EventTracing.Cpu;
using Microsoft.Windows.EventTracing.Processes;
using Microsoft.Windows.EventTracing.Symbols;

internal static class KernelReader
{
  private static readonly StringComparer Ci = StringComparer.OrdinalIgnoreCase;
  private sealed class Bucket
  {
    public long Count { get; set; }
    public double Ms { get; set; }
    public void Add(double ms) { Count++; Ms += ms; }
  }
  private sealed class ThreadTime
  {
    public int Tid { get; set; }
    public long Slices { get; set; }
    public double RunningMs { get; set; }
    public double ReadyMs { get; set; }
    public double WaitingMs { get; set; }
    public long MissingReady { get; set; }
    public long MissingWait { get; set; }
  }
  private static void Require(bool condition, string why)
  {
    if (!condition) throw new InvalidOperationException(why);
  }
  private static string Basename(string? path)
  {
    if (string.IsNullOrWhiteSpace(path)) return "UNRESOLVED_IMAGE";
    return path.Replace('\\', '/').Split('/').Last().ToLowerInvariant();
  }
  private static void Add(Dictionary<string, Bucket> dict, string key, double ms)
  {
    if (!dict.TryGetValue(key, out Bucket? bucket))
      dict[key] = bucket = new Bucket();
    bucket.Add(ms);
  }
  private static object Top(Dictionary<string, Bucket> source, int max = 100) =>
    source.OrderByDescending(kv => kv.Value.Ms).Take(max)
      .Select(kv => new { key = kv.Key, samples = kv.Value.Count,
                          cpu_sample_weight_ms = Math.Round(kv.Value.Ms, 6) }).ToArray();
  private static string ImageOf(StackFrame frame) => Basename(frame.Image?.Path);
  private static string SymbolOf(StackFrame frame)
  {
    string image = ImageOf(frame);
    string symbol = frame.Symbol?.ToString() ?? "NO_SYMBOL";
    return image + "!" + symbol;
  }
  private static int Main(string[] args)
  {
    Require(args.Length == 2, "usage: KernelReader <archived-stage-03.etl> <output-directory>");
    string tracePath = Path.GetFullPath(args[0]);
    string output = Path.GetFullPath(args[1]);
    Require(File.Exists(tracePath), "original ETL missing");
    Require(new FileInfo(tracePath).Length == 576716800,
            "not the expected original loss-free stage-03 ETL");
    Directory.CreateDirectory(output);
    using ITraceProcessor trace = TraceProcessor.Create(tracePath);
    IPendingResult<IProcessDataSource> pendingProcesses = trace.UseProcesses();
    IPendingResult<ICpuSampleDataSource> pendingSamples = trace.UseCpuSamplingData();
    IPendingResult<ICpuSchedulingDataSource> pendingSchedule = trace.UseCpuSchedulingData();
    IPendingResult<ISymbolDataSource> pendingSymbols = trace.UseSymbols();
    Console.WriteLine("RWF809_KERNEL_ETW_PROCESS_BEGIN trace=" + tracePath);
    trace.Process();
    Console.WriteLine("RWF809_KERNEL_ETW_PARSE_COMPLETE");
    var processes = pendingProcesses.Result.Processes
      .Where(p => string.Equals(Basename(p.ImageName), "rwf809_win32_nt.exe", StringComparison.OrdinalIgnoreCase)
               || (p.CommandLine ?? "").Contains("rwf809_win32_nt.exe", StringComparison.OrdinalIgnoreCase))
      .ToArray();
    Require(processes.Length > 0, "bench process absent from original ETW: cannot attribute kernel time");
    var ids = new HashSet<int>(processes.Select(p => (int)p.Id));
    var cpuData = pendingSamples.Result;
    var rawSamples = cpuData.Samples
      .Where(s => s.Process != null && ids.Contains((int)s.Process.Id)).ToArray();
    Require(rawSamples.Length > 0, "no samples from benchmark process: cannot attribute CPU");
    int withStack = rawSamples.Count(s => s.Stack != null && s.Stack.Frames.Count > 0);
    Require(withStack > 0, "benchmark CPU samples contain no kernel/user stacks");
    var symbolReport = new Dictionary<string, object?>();
    try
    {
      pendingSymbols.Result
        .LoadSymbolsForConsoleAsync(SymCachePath.Automatic, SymbolPath.Automatic)
        .GetAwaiter().GetResult();
      symbolReport["status"] = "loaded";
    }
    catch (Exception ex)
    {
      symbolReport["status"] = "failed";
      symbolReport["failure"] = ex.ToString();
      Console.WriteLine("RWF809_KERNEL_SYMBOL_LOAD_FAILED " + ex.Message);
      // Keep unsymbolized module/wait attribution. Do not claim function-level.
    }
    var topModule = new Dictionary<string, Bucket>(Ci);
    var inclusiveModule = new Dictionary<string, Bucket>(Ci);
    var topFunction = new Dictionary<string, Bucket>(Ci);
    var inclusiveFunction = new Dictionary<string, Bucket>(Ci);
    var kernelCategory = new Dictionary<string, Bucket>(Ci);
    long unresolvedTop = 0, withoutStack = 0;
    foreach (ICpuSample sample in rawSamples)
    {
      double ms = (double)sample.Weight.TotalMilliseconds;
      Require(double.IsFinite(ms) && ms >= 0, "invalid sampled CPU weight");
      StackFrame topFrame = sample.TopStackFrame;
      string primary = ImageOf(topFrame);
      if (primary == "UNRESOLVED_IMAGE") unresolvedTop++;
      Add(topModule, primary, ms);
      Add(topFunction, SymbolOf(topFrame), ms);
      var stack = sample.Stack;
      if (stack == null || stack.Frames.Count == 0) { withoutStack++; continue; }
      var images = new HashSet<string>(Ci);
      var functions = new HashSet<string>(Ci);
      foreach (StackFrame f in stack.Frames)
      {
        images.Add(ImageOf(f));
        functions.Add(SymbolOf(f));
      }
      foreach (string im in images) Add(inclusiveModule, im, ms);
      foreach (string fn in functions) Add(inclusiveFunction, fn, ms);
      bool hasNtfs = images.Contains("ntfs.sys");
      bool hasFlt = images.Contains("fltmgr.sys");
      bool hasWd = images.Contains("wdfilter.sys");
      bool hasKernel = images.Any(i => i is "ntoskrnl.exe" or "ntkrnlmp.exe");
      // Categories are NONEXCLUSIVE except first match; report method.
      string category = hasWd ? "wdfilter-stack" :
                        hasFlt ? "fltmgr-stack" :
                        hasNtfs ? "ntfs-stack" :
                        hasKernel ? "kernel-other-stack" : "user-or-unknown-stack";
      Add(kernelCategory, category, ms);
    }
    var scheduleData = pendingSchedule.Result;
    var slices = scheduleData.ThreadActivity
      .Where(s => s.Process != null && ids.Contains((int)s.Process.Id)).ToArray();
    Require(slices.Length > 0, "no CPU scheduling data for benchmark process");
    var threadTimes = new Dictionary<int, ThreadTime>();
    foreach (ICpuThreadActivity slice in slices)
    {
      int tid = (int)(slice.Thread?.Id ?? 0);
      if (!threadTimes.TryGetValue(tid, out ThreadTime? accum))
        threadTimes[tid] = accum = new ThreadTime { Tid = tid };
      accum.Slices++;
      double running = (double)slice.Duration.TotalMilliseconds;
      double ready = (double)slice.ReadyDuration.TotalMilliseconds;
      double waiting = (double)slice.WaitingDuration.TotalMilliseconds;
      Require(double.IsFinite(running) && running >= 0, "invalid running duration");
      Require(double.IsFinite(ready) && ready >= 0, "invalid ready duration");
      Require(double.IsFinite(waiting) && waiting >= 0, "invalid waiting duration");
      accum.RunningMs += running;
      accum.ReadyMs += ready;
      accum.WaitingMs += waiting;
      if (ready == 0) accum.MissingReady++;
      if (waiting == 0) accum.MissingWait++;
    }
    var result = new
    {
      provenance = new {
        origin_run = 43611008082L, replay_run = 43611781719L,
        original_source_sha = "d18a959ecc7ec551ae0b83d68b7155965edee4f4",
        original_etl_bytes = 576716800L,
        original_etl_sha256 = "4603ef3bf4d5eb1a2d1f31ceddc96f9d48fa7e8800f6ff5eba86fabbdb06914c",
        original_trace_stage = "03-fileio-cpu-stacks-scheduling",
        original_etw_reported_lost_events = 0,
        note = "Original 03 trace has substantial probe overhead; CPU sample weights and scheduling intervals are NOT exclusive syscall latency."
      },
      symbols = symbolReport,
      benchmark_processes = processes.Select(p => new { p.Id, p.ImageName, p.CommandLine }).ToArray(),
      cpu_sampling = new {
        samples = rawSamples.Length, with_stacks = withStack, without_stacks = withoutStack,
        unresolved_top_image = unresolvedTop,
        total_sampled_cpu_weight_ms = rawSamples.Sum(s => (double)s.Weight.TotalMilliseconds),
        top_instruction_modules = Top(topModule),
        inclusive_stack_modules_nonexclusive = Top(inclusiveModule),
        top_instruction_functions = Top(topFunction, 150),
        inclusive_stack_functions_nonexclusive = Top(inclusiveFunction, 150),
        stack_categories_exclusive_priority_wdfilter_fltmgr_ntfs_kernel_other = Top(kernelCategory)
      },
      scheduling = new {
        count = slices.Length,
        threads = threadTimes.Values.OrderByDescending(t => t.WaitingMs).ToArray(),
        total_thread_running_ms = threadTimes.Values.Sum(t => t.RunningMs),
        total_thread_ready_ms = threadTimes.Values.Sum(t => t.ReadyMs),
        total_thread_waiting_ms = threadTimes.Values.Sum(t => t.WaitingMs),
        note = "Summed thread-state intervals, not elapsed wall time. Check capture boundaries for truncated intervals."
      }
    };
    File.WriteAllText(Path.Combine(output, "kernel-analysis.json"),
      JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
    Console.WriteLine("RWF809_KERNEL_ETW_ATTRIBUTION_PASS samples=" + rawSamples.Length +
      " threads=" + threadTimes.Count +
      " symbol_status=" + symbolReport["status"] +
      " cpu_weight_ms=" + result.cpu_sampling.total_sampled_cpu_weight_ms +
      " wait_ms=" + result.scheduling.total_thread_waiting_ms);
    foreach (var row in topModule.OrderByDescending(x => x.Value.Ms).Take(14))
      Console.WriteLine("RWF809_KERNEL_MODULE " + row.Key + " cpu_ms=" + row.Value.Ms);
    foreach (var row in threadTimes.Values.OrderByDescending(x => x.WaitingMs).Take(12))
      Console.WriteLine("RWF809_KERNEL_THREAD tid=" + row.Tid + " run_ms=" + row.RunningMs +
        " ready_ms=" + row.ReadyMs + " wait_ms=" + row.WaitingMs);
    return 0;
  }
}
