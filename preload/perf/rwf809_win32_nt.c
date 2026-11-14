/* RWF 809: isolated exact-fixture Win32 vs direct NT read-only file APIs.
 * This is a benchmark, not a change to Git's production interface.
 * Byte validation is entirely OUTSIDE the timed benchmark interval.
 */
#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

enum { MAX_FILES = 8000, MAX_SIZE = 65536, PATH_CAP = 320, ROUNDS = 5 };
enum kind { WIN32_ALL, NT_OPEN, NT_READ, NT_CLOSE, NT_ALL,
            WIN32_META, NT_META, KIND_COUNT };
enum stage { ST_OPEN, ST_READ, ST_CLOSE, ST_META, ST_COUNT };
static const char *const names[KIND_COUNT] = {
  "win32_all", "nt_open_only", "nt_read_only", "nt_close_only",
  "nt_all", "win32_metadata", "nt_metadata"
};
struct shape { unsigned count, bytes; };
static const struct shape sizes[] = {
  {8000, 871}, {1024, 4096}, {256, 65536}, {128, 0}, {128, 1}
};
enum { SHAPE_COUNT = sizeof(sizes) / sizeof(sizes[0]) };
struct nt_network_info {
  LARGE_INTEGER created, accessed, written, changed, allocated, eof;
  ULONG attributes;
};
typedef NTSTATUS (NTAPI *nt_create_t)(
  PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES, PIO_STATUS_BLOCK,
  PLARGE_INTEGER, ULONG, ULONG, ULONG, ULONG, PVOID, ULONG);
typedef NTSTATUS (NTAPI *nt_read_t)(
  HANDLE, HANDLE, PVOID, PVOID, PIO_STATUS_BLOCK,
  PVOID, ULONG, PLARGE_INTEGER, PULONG);
typedef NTSTATUS (NTAPI *nt_close_t)(HANDLE);
typedef NTSTATUS (NTAPI *nt_meta_t)(
  POBJECT_ATTRIBUTES, struct nt_network_info *);
struct nt_fns {
  nt_create_t create;
  nt_read_t read;
  nt_close_t close;
  nt_meta_t meta;
};
static struct nt_fns nt;
struct file_path {
  wchar_t dos[PATH_CAP];
  wchar_t native[PATH_CAP + 4];
  UNICODE_STRING unicode;
  OBJECT_ATTRIBUTES object;
};
static struct file_path files[SHAPE_COUNT][MAX_FILES];
struct totals {
  unsigned long long ticks[ST_COUNT];
  unsigned long calls[ST_COUNT];
  unsigned long files;
  unsigned long long bytes;
};
struct worker_job {
  enum kind kind;
  unsigned shape, round, thread, threads;
  unsigned char *buffers;
  struct totals totals;
};
static LARGE_INTEGER frequency;
static void check(int ok, const char *message)
{
  if (!ok) {
    fprintf(stderr, "RWF809_NT_ERROR %s last_error=%lu\n",
            message, (unsigned long)GetLastError());
    fflush(stderr);
    ExitProcess(2);
  }
}
static void check_nt(NTSTATUS status, const char *message)
{
  if (status < 0) {
    fprintf(stderr, "RWF809_NT_ERROR %s ntstatus=0x%08lx\n",
            message, (unsigned long)status);
    fflush(stderr);
    ExitProcess(3);
  }
}
static unsigned long long clock_tick(void)
{
  LARGE_INTEGER t;
  check(QueryPerformanceCounter(&t), "QueryPerformanceCounter");
  return (unsigned long long)t.QuadPart;
}
static void record(struct totals *totals, enum stage s,
                   unsigned long long begin)
{
  unsigned long long now = clock_tick();
  check(now >= begin, "monotonic performance counter");
  totals->ticks[s] += now - begin;
  ++totals->calls[s];
}
static double ms(unsigned long long t)
{
  return 1000.0 * (double)t / (double)frequency.QuadPart;
}
static unsigned char pattern(unsigned index, unsigned offset)
{
  return (unsigned char)((index * 37U + offset * 13U + 41U) & 255U);
}
static void resolve_functions(void)
{
  HMODULE dll = GetModuleHandleW(L"ntdll.dll");
  check(dll != NULL, "ntdll loaded");
  nt.create = (nt_create_t)GetProcAddress(dll, "NtCreateFile");
  nt.read = (nt_read_t)GetProcAddress(dll, "NtReadFile");
  nt.close = (nt_close_t)GetProcAddress(dll, "NtClose");
  nt.meta = (nt_meta_t)GetProcAddress(dll, "NtQueryFullAttributesFile");
  check(nt.create && nt.read && nt.close && nt.meta,
        "required native NT exports");
}
static void prepare_name(unsigned shape, unsigned index)
{
  struct file_path *p = &files[shape][index];
  wchar_t relative[PATH_CAP];
  wchar_t *ignored = NULL;
  DWORD n;
  int count;
  count = swprintf_s(relative, PATH_CAP,
    L"rwf809-nt-files-%u\\folder-%02u\\tracked-%06u.bin",
    shape, index / 1000, index);
  check(count > 0 && count < PATH_CAP, "format file path");
  n = GetFullPathNameW(relative, PATH_CAP, p->dos, &ignored);
  check(n > 0 && n < PATH_CAP, "make absolute Win32 path");
  count = swprintf_s(p->native, PATH_CAP + 4, L"\\??\\%ls", p->dos);
  check(count > 0 && count < PATH_CAP + 4, "make NT path");
  p->unicode.Buffer = p->native;
  p->unicode.Length = (USHORT)(wcslen(p->native) * sizeof(WCHAR));
  p->unicode.MaximumLength = p->unicode.Length + sizeof(WCHAR);
  check((size_t)p->unicode.Length / sizeof(WCHAR) ==
        wcslen(p->native), "NT name length fits");
  p->object.Length = sizeof(p->object);
  p->object.RootDirectory = NULL;
  p->object.ObjectName = &p->unicode;
  p->object.Attributes = OBJ_CASE_INSENSITIVE | OBJ_INHERIT;
  p->object.SecurityDescriptor = NULL;
  p->object.SecurityQualityOfService = NULL;
}
static void fixture(unsigned shape)
{
  unsigned i, d, count = sizes[shape].count, length = sizes[shape].bytes;
  wchar_t directory[PATH_CAP];
  unsigned char data[MAX_SIZE];
  check(length <= MAX_SIZE, "fixture size limit");
  check(swprintf_s(directory, PATH_CAP, L"rwf809-nt-files-%u",
                   shape) > 0, "fixture root formatting");
  check(CreateDirectoryW(directory, NULL) ||
        GetLastError() == ERROR_ALREADY_EXISTS, "create fixture root");
  for (d = 0; d <= (count - 1) / 1000; d++) {
    check(swprintf_s(directory, PATH_CAP,
          L"rwf809-nt-files-%u\\folder-%02u", shape, d) > 0,
          "fixture directory formatting");
    check(CreateDirectoryW(directory, NULL) ||
          GetLastError() == ERROR_ALREADY_EXISTS,
          "create fixture folder");
  }
  for (i = 0; i < count; i++) {
    HANDLE f;
    DWORD written = 0;
    unsigned k;
    prepare_name(shape, i);
    for (k = 0; k < length; k++)
      data[k] = pattern(i, k);
    f = CreateFileW(files[shape][i].dos, GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    check(f != INVALID_HANDLE_VALUE, "fixture create file");
    if (length)
      check(WriteFile(f, data, length, &written, NULL) &&
            written == length, "fixture populate");
    check(CloseHandle(f), "fixture close file");
  }
}
static void process_one(struct worker_job *w, unsigned index)
{
  struct file_path *path = &files[w->shape][index];
  struct totals *total = &w->totals;
  const unsigned length = sizes[w->shape].bytes;
  unsigned char *dest = w->buffers + (size_t)index * (length ? length : 1);
  const enum kind kind = w->kind;
  unsigned long long start;
  HANDLE f = INVALID_HANDLE_VALUE;
  SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
  IO_STATUS_BLOCK iosb = {0};
  DWORD got = 0;
  NTSTATUS status;
  if (kind == WIN32_META || kind == NT_META) {
    if (kind == WIN32_META) {
      WIN32_FILE_ATTRIBUTE_DATA x;
      start = clock_tick();
      check(GetFileAttributesExW(path->dos, GetFileExInfoStandard, &x),
            "GetFileAttributesExW");
      record(total, ST_META, start);
      check((((uint64_t)x.nFileSizeHigh << 32) | x.nFileSizeLow) ==
            length, "Win32 metadata size");
    } else {
      struct nt_network_info x;
      start = clock_tick();
      status = nt.meta(&path->object, &x);
      record(total, ST_META, start);
      check_nt(status, "NtQueryFullAttributesFile");
      check((uint64_t)x.eof.QuadPart == length,
            "NT metadata size");
    }
  } else {
    if (kind == NT_OPEN || kind == NT_ALL) {
      start = clock_tick();
      status = nt.create(&f, GENERIC_READ, &path->object,
               &iosb, NULL, FILE_ATTRIBUTE_NORMAL,
               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
               1 /* FILE_OPEN */, 0x20 | 0x40
               /* FILE_SYNCHRONOUS_IO_NONALERT | FILE_NON_DIRECTORY_FILE */,
               NULL, 0);
      record(total, ST_OPEN, start);
      check_nt(status, "NtCreateFile");
      check(f != INVALID_HANDLE_VALUE && f != NULL,
            "native open valid handle");
    } else {
      start = clock_tick();
      f = CreateFileW(path->dos, GENERIC_READ,
          FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
          &sa, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
      record(total, ST_OPEN, start);
      check(f != INVALID_HANDLE_VALUE, "CreateFileW");
    }
    if (length) {
      if (kind == NT_READ || kind == NT_ALL) {
        memset(&iosb, 0, sizeof(iosb));
        start = clock_tick();
        status = nt.read(f, NULL, NULL, NULL, &iosb,
                         dest, length, NULL, NULL);
        record(total, ST_READ, start);
        check_nt(status, "NtReadFile");
        check(iosb.Information == length, "NtReadFile byte count");
        got = (DWORD)iosb.Information;
      } else {
        start = clock_tick();
        check(ReadFile(f, dest, length, &got, NULL),
              "ReadFile");
        record(total, ST_READ, start);
      }
      check(got == length, "complete file read");
    }
    if (kind == NT_CLOSE || kind == NT_ALL) {
      start = clock_tick();
      status = nt.close(f);
      record(total, ST_CLOSE, start);
      check_nt(status, "NtClose");
    } else {
      start = clock_tick();
      check(CloseHandle(f), "CloseHandle");
      record(total, ST_CLOSE, start);
    }
  }
  ++total->files;
  total->bytes += length;
}
static DWORD WINAPI work(void *context)
{
  struct worker_job *w = (struct worker_job *)context;
  const unsigned count = sizes[w->shape].count;
  unsigned i;
  for (i = w->thread; i < count; i += w->threads) {
    unsigned index = (i * 997u + 29u * w->round) % count;
    process_one(w, index);
  }
  return 0;
}
static void run(enum kind kind, unsigned shape, unsigned round,
                unsigned threads)
{
  struct worker_job jobs[4] = {0};
  HANDLE handles[4] = {0};
  struct totals combined = {0};
  const unsigned count = sizes[shape].count, length = sizes[shape].bytes;
  const size_t stride = length ? length : 1;
  unsigned char *data;
  unsigned long long begin, elapsed;
  unsigned t, s, i, k;
  check(threads == 1 || threads == 4, "thread count");
  check(count <= MAX_FILES && length <= MAX_SIZE, "shape dimensions");
  data = (unsigned char *)malloc((size_t)count * stride);
  check(data != NULL, "allocate outside timed benchmark");
  memset(data, 0, (size_t)count * stride);
  for (t = 0; t < threads; t++) {
    jobs[t].kind = kind;
    jobs[t].shape = shape;
    jobs[t].round = round;
    jobs[t].thread = t;
    jobs[t].threads = threads;
    jobs[t].buffers = data;
  }
  /* Only API workload runs between these QPC ticks. */
  begin = clock_tick();
  if (threads == 1) {
    check(work(&jobs[0]) == 0, "single worker");
  } else {
    for (t = 0; t < threads; t++) {
      handles[t] = CreateThread(NULL, 0, work, &jobs[t], 0, NULL);
      check(handles[t] != NULL, "create benchmark thread");
    }
    check(WaitForMultipleObjects(threads, handles, TRUE, INFINITE) ==
          WAIT_OBJECT_0, "wait for benchmark workers");
    for (t = 0; t < threads; t++) {
      DWORD exit_code;
      check(GetExitCodeThread(handles[t], &exit_code) && exit_code == 0,
            "verify worker exit");
      check(CloseHandle(handles[t]), "close thread handle");
    }
  }
  elapsed = clock_tick() - begin;

  /* CRITICAL: correctness checks occur AFTER the timed interval. */
  if (kind != WIN32_META && kind != NT_META) {
    for (i = 0; i < count; i++) {
      const unsigned char *file_data = data + (size_t)i * stride;
      for (k = 0; k < length; k++)
        check(file_data[k] == pattern(i, k),
              "read buffer byte-for-byte identity");
    }
  }
  for (t = 0; t < threads; t++) {
    combined.files += jobs[t].totals.files;
    combined.bytes += jobs[t].totals.bytes;
    for (s = 0; s < ST_COUNT; s++) {
      combined.ticks[s] += jobs[t].totals.ticks[s];
      combined.calls[s] += jobs[t].totals.calls[s];
    }
  }
  check(combined.files == count, "file count");
  check(combined.bytes == (unsigned long long)count * length,
        "total bytes");
  check(combined.calls[ST_META] ==
        ((kind == WIN32_META || kind == NT_META) ? count : 0),
        "metadata stage count");
  check(combined.calls[ST_OPEN] ==
        ((kind != WIN32_META && kind != NT_META) ? count : 0),
        "open stage count");
  check(combined.calls[ST_CLOSE] == combined.calls[ST_OPEN],
        "close stage count");
  check(combined.calls[ST_READ] ==
        ((kind != WIN32_META && kind != NT_META && length) ? count : 0),
        "read stage count");
  printf("RWF809_NT_CSV %s,%u,%u,%u,%u,%.6f,%.6f,%.6f,%.6f,%.6f,%lu,%llu\n",
         names[kind], shape, length, round, threads, ms(elapsed),
         ms(combined.ticks[ST_OPEN]), ms(combined.ticks[ST_READ]),
         ms(combined.ticks[ST_CLOSE]), ms(combined.ticks[ST_META]),
         combined.files, combined.bytes);
  fflush(stdout);
  free(data);
}
int main(void)
{
  unsigned shape, threads, round, k;
  check(QueryPerformanceFrequency(&frequency) &&
        frequency.QuadPart > 0, "counter frequency");
  resolve_functions();
  puts("RWF809_NT_HEADER mode,shape,size,round,threads,wall_ms,open_ms,read_ms,close_ms,meta_ms,files,bytes");
  for (shape = 0; shape < SHAPE_COUNT; shape++) {
    fixture(shape);
    for (k = 0; k < KIND_COUNT; k++)
      run((enum kind)k, shape, 999, 1);
    for (threads = 1; threads <= 4; threads += 3)
      for (round = 0; round < ROUNDS; round++)
        for (k = 0; k < KIND_COUNT; k++)
          run((enum kind)((k + round) % KIND_COUNT),
              shape, round, threads);
  }
  puts("RWF809_NT_PASS native Windows Win32-vs-NT all shape/mode/thread validation");
  return 0;
}
