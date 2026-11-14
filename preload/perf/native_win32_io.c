/*
 * RWF 809: standalone native-Windows file I/O timing.
 * No Git headers, source, runtime, DLL or process involved.
 * POSIX-like variants are explicitly analogues, not imported Git code.
 *
 * Same resolved paths, share/access/disposition and warmed file set.
 * Native = UTF-16 path + Win32 API.
 * CRT descriptor = Win32 open + _open_osfhandle + _close.
 * UTF-8 descriptor = UTF-8 conversion + Win32 open + CRT descriptor.
 *
 * Values are inclusive QPC elapsed times per operation, not kernel-only time.
 * Effects of access control, filter drivers and filesystem are still inside.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <wchar.h>
#include <io.h>
#include <fcntl.h>
#include <errno.h>
#include <inttypes.h>

#define FILE_COUNT 8000
#define DEFAULT_CYCLES 20
#define MAX_CYCLES 30
#define MAX_OBSERVATIONS (FILE_COUNT * MAX_CYCLES)
#define N_VARIANTS 5

enum variant {
  METADATA_NATIVE,
  METADATA_UTF8_CONVERT,
  OPEN_WIN32,
  OPEN_CRT_DESCRIPTOR,
  OPEN_UTF8_CRT_DESCRIPTOR,
};

static const char *variant_names[N_VARIANTS] = {
  "metadata_GetFileAttributesExW_wide",
  "metadata_UTF8_to_UTF16_plus_GetFileAttributesExW",
  "open_CreateFileW_CloseHandle",
  "open_CreateFileW_open_osfhandle_close",
  "open_UTF8_conversion_CreateFileW_open_osfhandle_close"
};

typedef struct {
  wchar_t wide[MAX_PATH];
  char utf8[MAX_PATH * 3];
} test_path;

static test_path paths[FILE_COUNT];
static uint64_t durations[N_VARIANTS][MAX_OBSERVATIONS];
static uint64_t totals[N_VARIANTS];
static unsigned counts[N_VARIANTS];
static LARGE_INTEGER qpc_freq;

static void fail(const char *what, int file, int variant, DWORD error) {
  fprintf(stderr, "BENCH_FAIL operation=%s file=%d variant=%d winerr=%lu errno=%d\n",
          what, file, variant, (unsigned long)error, errno);
  exit(2);
}

static uint64_t ticks(void) {
  LARGE_INTEGER t;
  if (!QueryPerformanceCounter(&t))
    fail("QueryPerformanceCounter", -1, -1, GetLastError());
  return (uint64_t)t.QuadPart;
}

static void get_path(int i, wchar_t *wide) {
  int rc;
  if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                           paths[i].utf8, -1, wide, MAX_PATH))
    fail("MultiByteToWideChar", i, -1, GetLastError());
  rc = wcscmp(paths[i].wide, wide);
  if (rc)
    fail("path_identity", i, -1, ERROR_INVALID_DATA);
}

static HANDLE open_file(const wchar_t *path, int i, int v) {
  HANDLE h = CreateFileW(path, GENERIC_READ,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                         NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
  if (h == INVALID_HANDLE_VALUE)
    fail("CreateFileW", i, v, GetLastError());
  return h;
}

static void check_metadata(const wchar_t *path, int i, int v) {
  WIN32_FILE_ATTRIBUTE_DATA attrs;
  if (!GetFileAttributesExW(path, GetFileExInfoStandard, &attrs))
    fail("GetFileAttributesExW", i, v, GetLastError());
  if (attrs.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
    fail("metadata_expected_file", i, v, ERROR_INVALID_DATA);
}

static void run_variant(enum variant v, int i, int record) {
  uint64_t t0, delta;
  wchar_t wide[MAX_PATH];
  const wchar_t *path = paths[i].wide;
  HANDLE h;
  int fd;
  if (record && counts[v] >= MAX_OBSERVATIONS)
    fail("maximum_observations", i, v, ERROR_BUFFER_OVERFLOW);

  t0 = ticks();
  switch (v) {
    case METADATA_NATIVE:
      check_metadata(path, i, v);
      break;
    case METADATA_UTF8_CONVERT:
      get_path(i, wide);
      check_metadata(wide, i, v);
      break;
    case OPEN_WIN32:
      h = open_file(path, i, v);
      if (!CloseHandle(h))
        fail("CloseHandle", i, v, GetLastError());
      break;
    case OPEN_CRT_DESCRIPTOR:
    case OPEN_UTF8_CRT_DESCRIPTOR:
      if (v == OPEN_UTF8_CRT_DESCRIPTOR) {
        get_path(i, wide);
        path = wide;
      }
      h = open_file(path, i, v);
      fd = _open_osfhandle((intptr_t)h, _O_RDONLY | _O_BINARY);
      if (fd < 0) {
        CloseHandle(h);
        fail("_open_osfhandle", i, v, GetLastError());
      }
      if (_close(fd) != 0)
        fail("_close", i, v, GetLastError());
      break;
    default:
      fail("variant", i, v, ERROR_INVALID_PARAMETER);
  }
  delta = ticks() - t0;
  if (record) {
    durations[v][counts[v]++] = delta;
    totals[v] += delta;
  }
}

static int compare_u64(const void *a, const void *b) {
  uint64_t x = *(const uint64_t *)a;
  uint64_t y = *(const uint64_t *)b;
  return x > y ? 1 : x < y ? -1 : 0;
}

static void prepare_paths(void) {
  wchar_t cwd[MAX_PATH];
  DWORD len = GetCurrentDirectoryW(MAX_PATH, cwd);
  int n;
  if (!len || len >= MAX_PATH)
    fail("GetCurrentDirectoryW", -1, -1, GetLastError());
  for (n = 0; n < FILE_COUNT; n++) {
    int length = swprintf(paths[n].wide, MAX_PATH,
                         L"%ls\\folder-%02d\\tracked-%06d.txt",
                         cwd, n / 1000, n);
    if (length < 1 || length >= MAX_PATH)
      fail("swprintf", n, -1, ERROR_BUFFER_OVERFLOW);
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
                             paths[n].wide, -1, paths[n].utf8,
                             sizeof(paths[n].utf8), NULL, NULL))
      fail("WideCharToMultiByte", n, -1, GetLastError());
    check_metadata(paths[n].wide, n, -1);
  }
}

int main(int argc, char **argv) {
  int cycles = DEFAULT_CYCLES;
  int n, round, v, offset;
  if (argc > 2) {
    fputs("usage: native_win32_io.exe [cycles <= 30]\n", stderr);
    return 2;
  }
  if (argc == 2) {
    char *end;
    long value = strtol(argv[1], &end, 10);
    if (!*argv[1] || *end || value <= 0 || value > MAX_CYCLES) {
      fputs("cycles must be 1..30\n", stderr);
      return 2;
    }
    cycles = (int)value;
  }
  if (!QueryPerformanceFrequency(&qpc_freq) || !qpc_freq.QuadPart)
    fail("QueryPerformanceFrequency", -1, -1, GetLastError());
  prepare_paths();

  /* Warm the same 8000 file set without recording. Rotate operation order. */
  for (n = 0; n < FILE_COUNT; n++)
    for (v = 0; v < N_VARIANTS; v++)
      run_variant((enum variant)((v + n) % N_VARIANTS), n, 0);

  for (round = 0; round < cycles; round++)
    for (n = 0; n < FILE_COUNT; n++) {
      offset = (round + n) % N_VARIANTS;
      for (v = 0; v < N_VARIANTS; v++)
        run_variant((enum variant)((v + offset) % N_VARIANTS), n, 1);
    }

  printf("{\"schema\":1,\"path_count\":%d,\"cycles\":%d,"
         "\"operations_per_variant\":%d,"
         "\"mode\":\"native_vs_equivalent_operation_sequences\","
         "\"access\":\"GENERIC_READ\",\"share\":\"READ_WRITE_DELETE\","
         "\"disposition\":\"OPEN_EXISTING\","
         "\"cache\":\"warmed\",\"variants\":[",
         FILE_COUNT, cycles, FILE_COUNT * cycles);

  for (v = 0; v < N_VARIANTS; v++) {
    unsigned count = counts[v], at95;
    double unit = 1000000.0 / (double)qpc_freq.QuadPart;
    if (count != (unsigned)(FILE_COUNT * cycles))
      fail("count_mismatch", -1, v, ERROR_INVALID_DATA);
    qsort(durations[v], count, sizeof(uint64_t), compare_u64);
    at95 = (count - 1) * 95 / 100;
    printf("%s{\"name\":\"%s\",\"calls\":%u,"
           "\"mean_us\":%.6f,\"median_us\":%.6f,"
           "\"p95_us\":%.6f,\"max_us\":%.6f}",
           v ? "," : "", variant_names[v], count,
           (double)totals[v] / count * unit,
           (double)durations[v][(count - 1) / 2] * unit,
           (double)durations[v][at95] * unit,
           (double)durations[v][count - 1] * unit);
  }
  puts("]}");
  return 0;
}
