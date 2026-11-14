/* RWF 809: direct Win32 calls on the exact same tracked files as Git.
 * Parameters match mingw_open_existing(O_RDONLY) and mingw_lstat.
 * The only timed span is the API call; CloseHandle is excluded.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

enum { FILE_COUNT = 8000, NAME_CAP = 128 };
enum mode { MODE_OPEN, MODE_ATTRS };
struct task {
  unsigned worker, workers;
  enum mode mode;
  volatile LONG failed;
  DWORD error;
  unsigned failed_index;
  unsigned long long elapsed_ticks, operation_count;
};
static wchar_t paths[FILE_COUNT][NAME_CAP];
static LARGE_INTEGER freq;

static DWORD WINAPI work(LPVOID context)
{
  struct task *task = (struct task *)context;
  unsigned i;
  for (i = task->worker; i < FILE_COUNT; i += task->workers) {
    LARGE_INTEGER start, end;
    BOOL success;
    if (!QueryPerformanceCounter(&start)) {
      task->failed = 1;
      task->error = GetLastError();
      task->failed_index = i;
      return 2;
    }
    if (task->mode == MODE_ATTRS) {
      WIN32_FILE_ATTRIBUTE_DATA attrs;
      success = GetFileAttributesExW(paths[i], GetFileExInfoStandard, &attrs);
      QueryPerformanceCounter(&end);
      if (!success || (attrs.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
        task->failed = 1;
        task->error = GetLastError();
        task->failed_index = i;
        return 2;
      }
    } else {
      SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
      HANDLE handle = CreateFileW(paths[i], GENERIC_READ,
          FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
          &sa, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
      QueryPerformanceCounter(&end);
      if (handle == INVALID_HANDLE_VALUE) {
        task->failed = 1;
        task->error = GetLastError();
        task->failed_index = i;
        return 2;
      }
      success = CloseHandle(handle);
      if (!success) {
        task->failed = 1;
        task->error = GetLastError();
        task->failed_index = i;
        return 2;
      }
    }
    if (end.QuadPart < start.QuadPart) {
      task->failed = 1;
      task->failed_index = i;
      return 2;
    }
    task->elapsed_ticks += (unsigned long long)(end.QuadPart - start.QuadPart);
    task->operation_count++;
  }
  return 0;
}

int main(int argc, char **argv)
{
  struct task tasks[4] = {0};
  HANDLE handles[4] = {0};
  LARGE_INTEGER start, end;
  unsigned workers, i;
  unsigned long long sum = 0, calls = 0;
  enum mode mode;
  if (argc != 3 || (strcmp(argv[1], "open") && strcmp(argv[1], "attrs")) ||
      (strcmp(argv[2], "1") && strcmp(argv[2], "4"))) {
    fputs("usage: rwf809_same_run.exe (open|attrs) (1|4)\n", stderr);
    return 2;
  }
  mode = !strcmp(argv[1], "open") ? MODE_OPEN : MODE_ATTRS;
  workers = (unsigned)atoi(argv[2]);
  if (!QueryPerformanceFrequency(&freq) || freq.QuadPart <= 0) {
    fputs("invalid performance counter frequency\n", stderr);
    return 2;
  }
  for (i = 0; i < FILE_COUNT; i++) {
    int n = swprintf_s(paths[i], NAME_CAP,
                       L"folder-%02u\\tracked-%06u.txt", i / 1000, i);
    if (n <= 0) {
      fputs("failed to construct fixture path\n", stderr);
      return 2;
    }
  }
  if (!QueryPerformanceCounter(&start)) return 2;
  for (i = 0; i < workers; i++) {
    tasks[i].worker = i;
    tasks[i].workers = workers;
    tasks[i].mode = mode;
    handles[i] = CreateThread(NULL, 0, work, &tasks[i], 0, NULL);
    if (!handles[i]) {
      fprintf(stderr, "CreateThread failed (%lu)\n", GetLastError());
      return 2;
    }
  }
  if (WaitForMultipleObjects(workers, handles, TRUE, INFINITE) != WAIT_OBJECT_0)
    return 2;
  if (!QueryPerformanceCounter(&end)) return 2;
  for (i = 0; i < workers; i++) {
    DWORD code;
    if (!GetExitCodeThread(handles[i], &code) || code != 0 || tasks[i].failed) {
      fprintf(stderr, "worker=%u index=%u error=%lu\n", i,
              tasks[i].failed_index, tasks[i].error);
      return 2;
    }
    if (!CloseHandle(handles[i])) return 2;
    sum += tasks[i].elapsed_ticks;
    calls += tasks[i].operation_count;
  }
  if (calls != FILE_COUNT) {
    fprintf(stderr, "operation count mismatch: %llu\n", calls);
    return 2;
  }
  printf("RWF809_NATIVE mode=%s threads=%u count=%llu api_ms=%.6f wall_ms=%.6f\n",
         argv[1], workers, calls,
         (double)sum * 1000.0 / (double)freq.QuadPart,
         (double)(end.QuadPart - start.QuadPart) * 1000.0 / (double)freq.QuadPart);
  return 0;
}
