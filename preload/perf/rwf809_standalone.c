/* RWF809 isolated Windows C experiment. No Git build.
 * Direct Win32 filesystem APIs vs representative UTF-8/CRT operations.
 * The actual Git shim remains independently measured by GIT_MINGW_PHASE_PROFILE.
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

enum { FILE_COUNT = 8000, REPEATS = 5, NAME_CAP = 192 };
enum mode { ATTR_WIDE, ATTR_UTF8, OPEN_WIDE, OPEN_UTF8_CRT, MODE_COUNT };
enum stage { ST_CONVERSION, ST_API, ST_DESCRIPTOR, ST_CLOSE, ST_COUNT };
static const char *const mode_name[] = {
  "attrs_wide", "attrs_utf8", "open_wide", "open_utf8_crt"
};
struct path_info { wchar_t wide[NAME_CAP]; char utf8[NAME_CAP]; };
struct stage_sum {
  unsigned long long ticks[ST_COUNT];
  unsigned long long calls[ST_COUNT];
};
static struct path_info paths[FILE_COUNT];
static LARGE_INTEGER frequency;
static void require(int condition, const char *message)
{
  if (!condition) {
    fprintf(stderr, "RWF809_STANDALONE_ERROR %s GetLastError=%lu\n",
            message, (unsigned long)GetLastError());
    exit(2);
  }
}
static long long tick(void)
{
  LARGE_INTEGER x;
  require(QueryPerformanceCounter(&x), "QueryPerformanceCounter");
  return x.QuadPart;
}
static void add(struct stage_sum *sum, enum stage s, long long start)
{
  long long delta = tick() - start;
  require(delta >= 0, "timer negative");
  sum->ticks[s] += (unsigned long long)delta;
  sum->calls[s]++;
}
static double ms(unsigned long long t)
{
  return 1000.0 * (double)t / (double)frequency.QuadPart;
}
static void fixture(void)
{
  unsigned n, d;
  require(CreateDirectoryW(L"bench-files", NULL), "create fixture root");
  for (d = 0; d < FILE_COUNT / 1000; d++) {
    wchar_t dirname[NAME_CAP];
    require(swprintf(dirname, NAME_CAP, L"bench-files\\folder-%02u", d) > 0,
            "dir path format");
    require(CreateDirectoryW(dirname, NULL), "create fixture directory");
  }
  for (n = 0; n < FILE_COUNT; n++) {
    HANDLE h;
    DWORD written = 0;
    const char bytes[] = "same payload for Git-like file open and metadata measurement\n";
    struct path_info *p = &paths[n];
    require(swprintf(p->wide, NAME_CAP,
                     L"bench-files\\folder-%02u\\tracked-%06u.txt",
                     n / 1000, n) > 0, "wide path format");
    require(sprintf_s(p->utf8, NAME_CAP,
                      "bench-files\\folder-%02u\\tracked-%06u.txt",
                      n / 1000, n) > 0, "utf8 path format");
    h = CreateFileW(p->wide, GENERIC_WRITE,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    require(h != INVALID_HANDLE_VALUE, "create file");
    require(WriteFile(h, bytes, (DWORD)strlen(bytes), &written, NULL) &&
            written == strlen(bytes), "write fixture file");
    require(CloseHandle(h), "close fixture file");
  }
}
static const wchar_t *get_path(struct stage_sum *sum, enum mode which,
                               struct path_info *p, wchar_t *converted)
{
  if (which == ATTR_WIDE || which == OPEN_WIDE)
    return p->wide;
  {
    int n;
    long long start = tick();
    n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, p->utf8, -1,
                            converted, NAME_CAP);
    add(sum, ST_CONVERSION, start);
    require(n > 0 && wcscmp(converted, p->wide) == 0,
            "UTF-8 conversion identity");
  }
  return converted;
}
static void probe_once(enum mode which, struct stage_sum *sum,
                       struct path_info *p)
{
  wchar_t converted[NAME_CAP];
  const wchar_t *name = get_path(sum, which, p, converted);
  long long start = tick();
  if (which == ATTR_WIDE || which == ATTR_UTF8) {
    WIN32_FILE_ATTRIBUTE_DATA data;
    BOOL ok = GetFileAttributesExW(name, GetFileExInfoStandard, &data);
    add(sum, ST_API, start);
    require(ok && !(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY),
            "GetFileAttributesExW existing file");
    return;
  }
  {
    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    HANDLE h = CreateFileW(
      name, GENERIC_READ,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      &sa, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    add(sum, ST_API, start);
    require(h != INVALID_HANDLE_VALUE, "CreateFileW existing file");
    if (which == OPEN_UTF8_CRT) {
      int fd;
      start = tick();
      fd = _open_osfhandle((intptr_t)h, _O_RDONLY | _O_BINARY);
      add(sum, ST_DESCRIPTOR, start);
      if (fd < 0) {
        CloseHandle(h);
        require(0, "_open_osfhandle");
      }
      start = tick();
      require(_close(fd) == 0, "CRT _close");
      add(sum, ST_CLOSE, start);
    } else {
      start = tick();
      require(CloseHandle(h), "Win32 CloseHandle");
      add(sum, ST_CLOSE, start);
    }
  }
}
int main(void)
{
  unsigned round, order, j;
  require(QueryPerformanceFrequency(&frequency) &&
          frequency.QuadPart > 0, "QueryPerformanceFrequency");
  fixture();
  puts("RWF809_STANDALONE_HEADER mode,round,count,elapsed_ms,conversion_ms,native_api_ms,descriptor_ms,close_ms");
  for (round = 0; round < REPEATS; round++) {
    for (order = 0; order < MODE_COUNT; order++) {
      enum mode which = (enum mode)((order + round) % MODE_COUNT);
      struct stage_sum result = {{0}, {0}};
      long long begin = tick();
      for (j = 0; j < FILE_COUNT; j++) {
        unsigned index = (j * 997U + 29U * round) % FILE_COUNT;
        probe_once(which, &result, &paths[index]);
      }
      {
        double elapsed = ms((unsigned long long)(tick() - begin));
        unsigned s;
        require(result.calls[ST_API] == FILE_COUNT, "count API operations");
        require(result.calls[ST_CLOSE] ==
                ((which == OPEN_WIDE || which == OPEN_UTF8_CRT) ?
                 FILE_COUNT : 0), "close count");
        printf("RWF809_STANDALONE_CSV %s,%u,%u,%.6f",
               mode_name[which], round, FILE_COUNT, elapsed);
        for (s = 0; s < ST_COUNT; s++)
          printf(",%.6f", ms(result.ticks[s]));
        puts("");
      }
    }
  }
  puts("RWF809_STANDALONE_PASS native operations and CRT comparisons");
  return 0;
}
