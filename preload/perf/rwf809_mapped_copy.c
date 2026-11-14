/* RWF #809: isolated Win32 read-vs-map-and-copy benchmark, not Git code.
 * Git-compatible use pattern: stat, open, read whole contents, close.
 * No Dobby hooks. QueryPerformanceCounter timing; fail-closed correctness.
 */
#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

enum { MAX_FILES = 8000, MAX_SIZE = 65536, NAME_CAP = 200, ROUNDS = 5 };
enum mode { CRT_READ, WIN32_READ, MAPPED_COPY, MODE_COUNT };
enum stage { OPEN, DESCRIPTOR, SIZE_QUERY, CREATE_MAPPING, MAP_VIEW, READ_COPY,
             UNMAP, CLOSE_MAPPING, CLOSE_FILE, STAGE_COUNT };
static const char *mode_names[] = {"crt_read", "win32_read", "mapped_copy"};
static const char *stage_names[] = {"open", "descriptor", "size_query",
  "create_mapping", "map_view", "read_or_copy", "unmap", "close_mapping",
  "close_file"};
struct shape { unsigned count; unsigned length; };
static const struct shape shapes[] = {
  {8000, 871}, {1024, 4096}, {256, 65536}, {128, 0}, {128, 1}
};
struct totals { unsigned long long ticks[STAGE_COUNT]; unsigned long calls[STAGE_COUNT];
                unsigned long files; unsigned long long bytes; };
struct task { enum mode which; unsigned shape; unsigned round;
              unsigned thread_id; unsigned threads; struct totals total; };
struct adapter { HANDLE file, mapping; const unsigned char *view;
                 unsigned long long length, offset; };
static LARGE_INTEGER frequency;
static void check(int good, const char *what)
{
  if (!good) {
    fprintf(stderr, "RWF809_MMAP_ERROR %s GetLastError=%lu\n",
            what, (unsigned long)GetLastError());
    fflush(stderr);
    ExitProcess(2);
  }
}
static unsigned long long tick(void)
{
  LARGE_INTEGER val;
  check(QueryPerformanceCounter(&val) != 0, "QueryPerformanceCounter");
  return (unsigned long long)val.QuadPart;
}
static void end_stage(struct totals *sum, enum stage stage,
                      unsigned long long before)
{
  unsigned long long after = tick();
  check(after >= before, "clock backward");
  sum->ticks[stage] += after - before;
  ++sum->calls[stage];
}
static double to_ms(unsigned long long ticks)
{
  return (double)ticks * 1000.0 / (double)frequency.QuadPart;
}
static unsigned char expected(unsigned index, unsigned byte)
{
  return (unsigned char)((index * 37u + byte * 13u + 41u) & 255u);
}
static void name_for(unsigned s, unsigned index, wchar_t *out)
{
  int n = swprintf_s(out, NAME_CAP,
    L"rwf809-mapped-fixture-%u\\folder-%02u\\tracked-%06u.bin",
    s, index / 1000, index);
  check(n > 0 && n < NAME_CAP, "format path");
}
static void make_fixture(unsigned s)
{
  unsigned i, d, length = shapes[s].length;
  wchar_t dirname[NAME_CAP], pathname[NAME_CAP];
  unsigned char buffer[MAX_SIZE];
  check(swprintf_s(dirname, NAME_CAP, L"rwf809-mapped-fixture-%u", s) > 0,
        "format fixture root");
  check(CreateDirectoryW(dirname, NULL) != 0 ||
        GetLastError() == ERROR_ALREADY_EXISTS, "create fixture root");
  for (d = 0; d <= (shapes[s].count - 1) / 1000; d++) {
    check(swprintf_s(dirname, NAME_CAP,
          L"rwf809-mapped-fixture-%u\\folder-%02u", s, d) > 0,
          "format fixture folder");
    check(CreateDirectoryW(dirname, NULL) != 0 ||
          GetLastError() == ERROR_ALREADY_EXISTS, "create fixture folder");
  }
  for (i = 0; i < shapes[s].count; i++) {
    HANDLE f;
    unsigned j;
    DWORD nwritten = 0;
    name_for(s, i, pathname);
    for (j = 0; j < length; j++)
      buffer[j] = expected(i, j);
    f = CreateFileW(pathname, GENERIC_WRITE,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    check(f != INVALID_HANDLE_VALUE, "create fixture file");
    if (length)
      check(WriteFile(f, buffer, (DWORD)length, &nwritten, NULL) != 0 &&
            nwritten == length, "populate fixture file");
    check(CloseHandle(f) != 0, "close fixture creation handle");
  }
}
static DWORD adapter_read(struct adapter *a, unsigned char *out, DWORD wanted)
{
  unsigned long long available;
  DWORD n;
  check(a->offset <= a->length, "adapter offset in bounds");
  available = a->length - a->offset;
  n = (DWORD)(available < wanted ? available : wanted);
  if (n) {
    check(a->view != NULL, "nonempty read has view");
    memcpy(out, a->view + a->offset, n);
  }
  a->offset += n;
  return n;
}
static void probe(struct task *t, unsigned index, unsigned char *buffer)
{
  struct totals *sum = &t->total;
  const unsigned length = shapes[t->shape].length;
  wchar_t path[NAME_CAP];
  SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
  struct adapter a = {INVALID_HANDLE_VALUE, NULL, NULL, 0, 0};
  unsigned long long before;
  DWORD got = 0;
  int fd = -1;
  unsigned j;
  name_for(t->shape, index, path);
  before = tick();
  a.file = CreateFileW(path, GENERIC_READ,
    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
    &sa, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
  end_stage(sum, OPEN, before);
  check(a.file != INVALID_HANDLE_VALUE, "CreateFileW read-only");
  if (t->which == CRT_READ) {
    before = tick();
    fd = _open_osfhandle((intptr_t)a.file, _O_RDONLY | _O_BINARY);
    end_stage(sum, DESCRIPTOR, before);
    check(fd >= 0, "_open_osfhandle");
    before = tick();
    {
      int n = _read(fd, buffer, (unsigned)length);
      check(n >= 0, "CRT _read");
      got = (DWORD)n;
    }
    end_stage(sum, READ_COPY, before);
    before = tick();
    check(_close(fd) == 0, "CRT _close");
    end_stage(sum, CLOSE_FILE, before);
  } else if (t->which == WIN32_READ) {
    before = tick();
    check(ReadFile(a.file, buffer, (DWORD)length, &got, NULL) != 0,
          "ReadFile");
    end_stage(sum, READ_COPY, before);
    before = tick();
    check(CloseHandle(a.file) != 0, "CloseHandle read file");
    end_stage(sum, CLOSE_FILE, before);
  } else {
    LARGE_INTEGER size;
    before = tick();
    check(GetFileSizeEx(a.file, &size) != 0, "GetFileSizeEx");
    end_stage(sum, SIZE_QUERY, before);
    check(size.QuadPart >= 0 && (unsigned long long)size.QuadPart == length,
          "mapped input size matches fixture");
    a.length = (unsigned long long)size.QuadPart;
    if (a.length) {
      before = tick();
      a.mapping = CreateFileMappingW(a.file, NULL, PAGE_READONLY,
                                    0, 0, NULL);
      end_stage(sum, CREATE_MAPPING, before);
      check(a.mapping != NULL, "CreateFileMappingW");
      before = tick();
      a.view = (const unsigned char *)MapViewOfFile(a.mapping,
        FILE_MAP_READ, 0, 0, 0);
      end_stage(sum, MAP_VIEW, before);
      check(a.view != NULL, "MapViewOfFile");
    }
    before = tick();
    got = adapter_read(&a, buffer, (DWORD)length);
    end_stage(sum, READ_COPY, before);
    check(adapter_read(&a, buffer, 1) == 0, "adapter EOF");
    if (a.view) {
      before = tick();
      check(UnmapViewOfFile(a.view) != 0, "UnmapViewOfFile");
      end_stage(sum, UNMAP, before);
    }
    if (a.mapping) {
      before = tick();
      check(CloseHandle(a.mapping) != 0, "close mapping handle");
      end_stage(sum, CLOSE_MAPPING, before);
    }
    before = tick();
    check(CloseHandle(a.file) != 0, "close underlying mapped file");
    end_stage(sum, CLOSE_FILE, before);
  }
  check(got == length, "read entire file into caller-provided buffer");
  for (j = 0; j < length; j++)
    check(buffer[j] == expected(index, j), "buffer byte identity");
  sum->files++;
  sum->bytes += got;
}
static DWORD WINAPI worker(void *arg)
{
  struct task *t = (struct task *)arg;
  const unsigned count = shapes[t->shape].count;
  unsigned char buffer[MAX_SIZE + 1];
  unsigned i;
  for (i = t->thread_id; i < count; i += t->threads) {
    unsigned index = (i * 997u + t->round * 29u) % count;
    probe(t, index, buffer);
  }
  return 0;
}
static void benchmark(enum mode which, unsigned shape, unsigned round,
                      unsigned threads)
{
  struct task tasks[4] = {0};
  HANDLE handles[4] = {0};
  struct totals sum = {0};
  unsigned i, j;
  unsigned long long before, elapsed;
  check(threads == 1 || threads == 4, "supported thread count");
  before = tick();
  if (threads == 1) {
    tasks[0].which = which; tasks[0].shape = shape;
    tasks[0].round = round; tasks[0].threads = 1;
    check(worker(&tasks[0]) == 0, "single-thread worker");
  } else {
    for (i = 0; i < threads; i++) {
      tasks[i].which = which; tasks[i].shape = shape;
      tasks[i].round = round; tasks[i].thread_id = i;
      tasks[i].threads = threads;
      handles[i] = CreateThread(NULL, 0, worker, &tasks[i], 0, NULL);
      check(handles[i] != NULL, "CreateThread");
    }
    check(WaitForMultipleObjects(threads, handles, TRUE, INFINITE) ==
          WAIT_OBJECT_0, "WaitForMultipleObjects");
    for (i = 0; i < threads; i++) {
      DWORD code;
      check(GetExitCodeThread(handles[i], &code) && code == 0,
            "worker exit status");
      check(CloseHandle(handles[i]), "close thread handle");
    }
  }
  elapsed = tick() - before;
  for (i = 0; i < threads; i++) {
    sum.files += tasks[i].total.files;
    sum.bytes += tasks[i].total.bytes;
    for (j = 0; j < STAGE_COUNT; j++) {
      sum.calls[j] += tasks[i].total.calls[j];
      sum.ticks[j] += tasks[i].total.ticks[j];
    }
  }
  check(sum.files == shapes[shape].count, "all fixture files processed");
  check(sum.bytes == (unsigned long long)sum.files * shapes[shape].length,
        "all fixture bytes copied");
  check(sum.calls[OPEN] == sum.files && sum.calls[READ_COPY] == sum.files &&
        sum.calls[CLOSE_FILE] == sum.files, "required stage counts");
  if (which == MAPPED_COPY) {
    check(sum.calls[SIZE_QUERY] == sum.files, "mapped metadata count");
    check(sum.calls[MAP_VIEW] == (shapes[shape].length ? sum.files : 0),
          "mapped view count");
  }
  printf("RWF809_MMAP_CSV %s,%u,%u,%u,%u,%.6f",
         mode_names[which], shape, shapes[shape].length, round, threads,
         to_ms(elapsed));
  for (i = 0; i < STAGE_COUNT; i++)
    printf(",%.6f", to_ms(sum.ticks[i]));
  printf(",%lu,%llu\n", sum.files, sum.bytes);
  fflush(stdout);
}
int main(void)
{
  unsigned shape, threads, round, k;
  check(QueryPerformanceFrequency(&frequency) != 0 &&
        frequency.QuadPart > 0, "QueryPerformanceFrequency");
  puts("RWF809_MMAP_HEADER mode,shape,size,round,threads,wall_ms,open_ms,descriptor_ms,size_query_ms,create_mapping_ms,map_view_ms,read_or_copy_ms,unmap_ms,close_mapping_ms,close_file_ms,files,bytes");
  for (shape = 0; shape < sizeof(shapes) / sizeof(shapes[0]); shape++) {
    make_fixture(shape);
    /* One fully checked warm-up per method, excluded from five rounds. */
    for (k = 0; k < MODE_COUNT; k++)
      benchmark((enum mode)k, shape, 999, 1);
    for (threads = 1; threads <= 4; threads += 3) {
      for (round = 0; round < ROUNDS; round++) {
        for (k = 0; k < MODE_COUNT; k++) {
          enum mode which = (enum mode)((round + k) % MODE_COUNT);
          benchmark(which, shape, round, threads);
        }
      }
    }
  }
  puts("RWF809_MMAP_PASS all sizes/modes/threads validated");
  return 0;
}
