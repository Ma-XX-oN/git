#define IO_TRACE_INTERNAL
#include "git-compat-util.h"
#include "io-trace.h"

#if defined(GIT_WINDOWS_NATIVE)
#include <windows.h>
#elif defined(__APPLE__)
#include <pthread.h>
#elif defined(__linux__)
#include <sys/syscall.h>
#endif

#define IO_TRACE_PATH_MAX 1024
#define IO_TRACE_PATH_TRUNCATED 1

struct io_trace_header {
  char magic[8];
  uint32_t version;
  uint32_t endian;
  uint32_t record_size;
  uint32_t reserved;
  uint64_t tick_frequency;
  uint64_t pid;
};

struct io_trace_record {
  uint64_t ticks;
  uint64_t value;
  uint64_t argument;
  uint64_t handle;
  uint64_t tid;
  uint32_t pid;
  int32_t status;
  uint16_t event;
  uint16_t flags;
  uint32_t path_length;
  uint32_t reserved;
};

struct io_trace_pair {
  struct io_trace_record event[2];
  char path[IO_TRACE_PATH_MAX];
};

typedef char io_trace_header_must_be_40_bytes[
  sizeof(struct io_trace_header) == 40 ? 1 : -1];
typedef char io_trace_record_must_be_64_bytes[
  sizeof(struct io_trace_record) == 64 ? 1 : -1];

static FILE *io_trace_file;

static uint64_t io_trace_ticks(void)
{
#if defined(GIT_WINDOWS_NATIVE)
  LARGE_INTEGER value;
  if (!QueryPerformanceCounter(&value))
    return 0;
  return (uint64_t)value.QuadPart;
#else
  struct timespec t;
  if (clock_gettime(CLOCK_MONOTONIC, &t))
    return 0;
  return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
#endif
}

static uint64_t io_trace_frequency(void)
{
#if defined(GIT_WINDOWS_NATIVE)
  LARGE_INTEGER frequency;
  if (!QueryPerformanceFrequency(&frequency))
    return 0;
  return (uint64_t)frequency.QuadPart;
#else
  return 1000000000ULL;
#endif
}

static uint64_t io_trace_thread(void)
{
#if defined(GIT_WINDOWS_NATIVE)
  return GetCurrentThreadId();
#elif defined(__APPLE__)
  uint64_t id = 0;
  pthread_threadid_np(NULL, &id);
  return id;
#elif defined(__linux__)
  return (uint32_t)syscall(SYS_gettid);
#else
  return 0;
#endif
}

static void io_trace_single(enum io_trace_event_type kind)
{
  struct io_trace_record record = { 0 };

  record.event = kind;
  record.pid = (uint32_t)getpid();
  record.tid = io_trace_thread();
  record.ticks = io_trace_ticks();
  fwrite(&record, 1, sizeof(record), io_trace_file);
}

static void io_trace_shutdown(void)
{
  FILE *file = io_trace_file;
  int saved_errno = errno;
  int failed;

  if (!file)
    return;

  io_trace_single(IO_PROCESS_EXIT);
  io_trace_file = NULL;
  failed = fflush(file) != 0 || ferror(file);
  if (fclose(file) || failed)
    fputs("git: binary I/O trace output was incomplete\n", stderr);
  errno = saved_errno;
}

int io_trace_begin(const char *option, const char *value)
{
  struct io_trace_header header = { { 'G', 'I', 'T', 'I', 'O', '0', '0', '1' },
                                    1, 0x01020304, 64, 0, 0, 0 };
  char name[32];
  const char *filename = value;
  FILE *file;
  int fd;
  size_t i;
  int flags = O_WRONLY | O_CREAT | O_EXCL;

  if (io_trace_file || !value || !*value) {
    errno = EINVAL;
    return -1;
  }

  if (!strcmp(option, "--log-id")) {
    if (strlen(value) != 16) {
      errno = EINVAL;
      return -1;
    }
    for (i = 0; i < 16; i++) {
      unsigned char c = (unsigned char)value[i];
      if (!((c >= '0' && c <= '9') ||
            (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))) {
        errno = EINVAL;
        return -1;
      }
      name[4 + i] = (c >= 'A' && c <= 'F') ? c + ('a' - 'A') : c;
    }
    memcpy(name, "git-", 4);
    memcpy(name + 20, ".log", 5);
    filename = name;
  } else if (strcmp(option, "--log-fn")) {
    errno = EINVAL;
    return -1;
  }

#ifdef O_CLOEXEC
  flags |= O_CLOEXEC;
#endif
  fd = open(filename, flags, 0600);
  if (fd < 0)
    return -1;
  file = fdopen(fd, "wb");
  if (!file) {
    int saved_errno = errno;
    close(fd);
    errno = saved_errno;
    return -1;
  }
  header.tick_frequency = io_trace_frequency();
  header.pid = (uint32_t)getpid();
  if (!header.tick_frequency ||
      fwrite(&header, 1, sizeof(header), file) != sizeof(header) ||
      atexit(io_trace_shutdown)) {
    int saved_errno = errno ? errno : EIO;
    fclose(file);
    errno = saved_errno;
    return -1;
  }

  io_trace_file = file;
  io_trace_single(IO_PROCESS_ENTER);
  return 0;
}

static void io_trace_copy_path(struct io_trace_pair *pair,
                               struct io_trace_record *record,
                               const char *path)
{
  size_t len;

  if (!path)
    return;
  len = strlen(path);
  if (len >= IO_TRACE_PATH_MAX) {
    len = IO_TRACE_PATH_MAX;
    record->flags |= IO_TRACE_PATH_TRUNCATED;
  }
  memcpy(pair->path, path, len);
  record->path_length = (uint32_t)len;
}

static void io_trace_prepare(struct io_trace_pair *pair,
                             enum io_trace_event_type kind,
                             const char *path,
                             uint64_t handle, uint64_t argument)
{
  memset(pair->event, 0, sizeof(pair->event));
  pair->event[0].event = kind;
  pair->event[0].pid = (uint32_t)getpid();
  pair->event[0].tid = io_trace_thread();
  pair->event[0].handle = handle;
  pair->event[0].argument = argument;
  io_trace_copy_path(pair, &pair->event[0], path);
  pair->event[1] = pair->event[0];
  pair->event[1].event = kind + 1;
  pair->event[1].path_length = 0;
  pair->event[1].flags = 0;
}

static void io_trace_record_pair(struct io_trace_pair *pair,
                                 int64_t result, int error,
                                 uint64_t exit_handle,
                                 const char *returned_path)
{
  size_t len;

  pair->event[1].value = (uint64_t)result;
  pair->event[1].status = error;
  pair->event[1].handle = exit_handle;
  if (returned_path)
    io_trace_copy_path(pair, &pair->event[1], returned_path);
  len = pair->event[0].path_length + pair->event[1].path_length;
  /*
   * The pair is emitted with one fwrite() call. Each FILE* operation
   * is locked by stdio, preventing threads from interleaving pairs.
   * No log write occurs between the two timestamps.
   */
  fwrite(pair, 1, sizeof(pair->event) + len, io_trace_file);
}

int io_trace_open(const char *path, int flags, ...)
{
  struct io_trace_pair pair;
  mode_t mode = 0;
  va_list ap;
  int result, saved_errno;

  va_start(ap, flags);
  if (flags & O_CREAT
#ifdef O_TMPFILE
      || (flags & O_TMPFILE) == O_TMPFILE
#endif
      )
    mode = va_arg(ap, int);
  va_end(ap);
  if (!io_trace_file)
    return open(path, flags, mode);

  io_trace_prepare(&pair, IO_OPEN_ENTER, path, 0, flags);
  pair.event[0].ticks = io_trace_ticks();
  result = open(path, flags, mode);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       result >= 0 ? (uint64_t)result : 0, NULL);
  errno = saved_errno;
  return result;
}

ssize_t io_trace_read(int fd, void *buf, size_t size)
{
  struct io_trace_pair pair;
  ssize_t result;
  int saved_errno;

  if (!io_trace_file)
    return read(fd, buf, size);
  io_trace_prepare(&pair, IO_READ_ENTER, NULL, fd, size);
  pair.event[0].ticks = io_trace_ticks();
  result = read(fd, buf, size);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       fd, NULL);
  errno = saved_errno;
  return result;
}

ssize_t io_trace_write(int fd, const void *buf, size_t size)
{
  struct io_trace_pair pair;
  ssize_t result;
  int saved_errno;

  if (!io_trace_file)
    return write(fd, buf, size);
  io_trace_prepare(&pair, IO_WRITE_ENTER, NULL, fd, size);
  pair.event[0].ticks = io_trace_ticks();
  result = write(fd, buf, size);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       fd, NULL);
  errno = saved_errno;
  return result;
}

int io_trace_close(int fd)
{
  struct io_trace_pair pair;
  int result, saved_errno;

  if (!io_trace_file)
    return close(fd);
  io_trace_prepare(&pair, IO_CLOSE_ENTER, NULL, fd, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = close(fd);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       fd, NULL);
  errno = saved_errno;
  return result;
}

#define IO_TRACE_STAT_WRAPPER(name, kind, function)                   \
int name(const char *path, struct stat *result)                       \
{                                                                    \
  struct io_trace_pair pair;                                         \
  int ret, saved_errno;                                              \
  if (!io_trace_file)                                                \
    return function(path, result);                                   \
  io_trace_prepare(&pair, kind, path, 0, 0);                         \
  pair.event[0].ticks = io_trace_ticks();                            \
  ret = function(path, result);                                      \
  saved_errno = errno;                                               \
  pair.event[1].ticks = io_trace_ticks();                            \
  io_trace_record_pair(&pair, ret, ret < 0 ? saved_errno : 0, 0, NULL); \
  errno = saved_errno;                                               \
  return ret;                                                        \
}

IO_TRACE_STAT_WRAPPER(io_trace_stat, IO_STAT_ENTER, stat)
IO_TRACE_STAT_WRAPPER(io_trace_lstat, IO_LSTAT_ENTER, lstat)

int io_trace_fstat(int fd, struct stat *result)
{
  struct io_trace_pair pair;
  int ret, saved_errno;

  if (!io_trace_file)
    return fstat(fd, result);
  io_trace_prepare(&pair, IO_FSTAT_ENTER, NULL, fd, 0);
  pair.event[0].ticks = io_trace_ticks();
  ret = fstat(fd, result);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, ret, ret < 0 ? saved_errno : 0, fd, NULL);
  errno = saved_errno;
  return ret;
}

DIR *io_trace_opendir(const char *path)
{
  struct io_trace_pair pair;
  DIR *result;
  int saved_errno;

  if (!io_trace_file)
    return opendir(path);
  io_trace_prepare(&pair, IO_OPENDIR_ENTER, path, 0, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = opendir(path);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, (intptr_t)result,
                       !result ? saved_errno : 0,
                       (uintptr_t)result, NULL);
  errno = saved_errno;
  return result;
}

struct dirent *io_trace_readdir(DIR *dir)
{
  struct io_trace_pair pair;
  struct dirent *result;
  int saved_errno;

  if (!io_trace_file)
    return readdir(dir);
  io_trace_prepare(&pair, IO_READDIR_ENTER, NULL, (uintptr_t)dir, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = readdir(dir);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, (intptr_t)result,
                       !result ? saved_errno : 0,
                       (uintptr_t)dir, result ? result->d_name : NULL);
  errno = saved_errno;
  return result;
}

int io_trace_closedir(DIR *dir)
{
  struct io_trace_pair pair;
  int result, saved_errno;

  if (!io_trace_file)
    return closedir(dir);
  io_trace_prepare(&pair, IO_CLOSEDIR_ENTER, NULL, (uintptr_t)dir, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = closedir(dir);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       (uintptr_t)dir, NULL);
  errno = saved_errno;
  return result;
}

FILE *io_trace_fopen(const char *path, const char *mode)
{
  struct io_trace_pair pair;
  FILE *result;
  int saved_errno;

  if (!io_trace_file)
    return fopen(path, mode);
  io_trace_prepare(&pair, IO_FOPEN_ENTER, path, 0, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = fopen(path, mode);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, (intptr_t)result,
                       !result ? saved_errno : 0,
                       (uintptr_t)result, NULL);
  errno = saved_errno;
  return result;
}

size_t io_trace_fread(void *ptr, size_t size, size_t count, FILE *stream)
{
  struct io_trace_pair pair;
  size_t result;
  int saved_errno, error;

  if (!io_trace_file)
    return fread(ptr, size, count, stream);
  io_trace_prepare(&pair, IO_FREAD_ENTER, NULL, (uintptr_t)stream,
                   size && count <= SIZE_MAX / size ? size * count : SIZE_MAX);
  pair.event[0].ticks = io_trace_ticks();
  result = fread(ptr, size, count, stream);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  error = result != count && ferror(stream) ? saved_errno : 0;
  io_trace_record_pair(&pair,
                       size && result <= SIZE_MAX / size ? result * size : SIZE_MAX,
                       error,
                       (uintptr_t)stream, NULL);
  errno = saved_errno;
  return result;
}

size_t io_trace_fwrite(const void *ptr, size_t size, size_t count, FILE *stream)
{
  struct io_trace_pair pair;
  size_t result;
  int saved_errno, error;

  if (!io_trace_file)
    return fwrite(ptr, size, count, stream);
  io_trace_prepare(&pair, IO_FWRITE_ENTER, NULL, (uintptr_t)stream,
                   size && count <= SIZE_MAX / size ? size * count : SIZE_MAX);
  pair.event[0].ticks = io_trace_ticks();
  result = fwrite(ptr, size, count, stream);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  error = result != count && ferror(stream) ? saved_errno : 0;
  io_trace_record_pair(&pair, result * size, error,
                       (uintptr_t)stream, NULL);
  errno = saved_errno;
  return result;
}

int io_trace_fclose(FILE *stream)
{
  struct io_trace_pair pair;
  int result, saved_errno;

  if (!io_trace_file)
    return fclose(stream);
  io_trace_prepare(&pair, IO_FCLOSE_ENTER, NULL, (uintptr_t)stream, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = fclose(stream);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       (uintptr_t)stream, NULL);
  errno = saved_errno;
  return result;
}
