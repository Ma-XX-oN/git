#include "git-compat-util.h"
#include "io-trace.h"
#ifndef GIT_WINDOWS_NATIVE
#include <dlfcn.h>
#endif

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

/* Dobby returns the unmodified function trampoline in each orig_* slot. */
static int (*orig_open)(const char *, int, ...) = open;
static ssize_t (*orig_read)(int, void *, size_t) = read;
static ssize_t (*orig_write)(int, const void *, size_t) = write;
static int (*orig_close)(int) = close;
static int (*orig_stat)(const char *, struct stat *) = stat;
static int (*orig_lstat)(const char *, struct stat *) = lstat;
static int (*orig_fstat)(int, struct stat *) = fstat;
static DIR *(*orig_opendir)(const char *) = opendir;
static struct dirent *(*orig_readdir)(DIR *) = readdir;
static int (*orig_closedir)(DIR *) = closedir;
static FILE *(*orig_fopen)(const char *, const char *) = fopen;
static size_t (*orig_fread)(void *, size_t, size_t, FILE *) = fread;
static size_t (*orig_fwrite)(const void *, size_t, size_t, FILE *) = fwrite;
static int (*orig_fclose)(FILE *) = fclose;

/* The logger itself and nested implementations never produce I/O records. */
#if defined(_MSC_VER)
# define IO_TRACE_TLS __declspec(thread)
#else
# define IO_TRACE_TLS __thread
#endif
static IO_TRACE_TLS int io_trace_in_logger;
static IO_TRACE_TLS unsigned io_trace_call_depth;
static int io_trace_active;

static int hook_open(const char *path, int flags, ...);
static ssize_t hook_read(int fd, void *buf, size_t size);
static ssize_t hook_write(int fd, const void *buf, size_t size);
static int hook_close(int fd);
static int hook_stat(const char *path, struct stat *st);
static int hook_lstat(const char *path, struct stat *st);
static int hook_fstat(int fd, struct stat *st);
static DIR *hook_opendir(const char *path);
static struct dirent *hook_readdir(DIR *dir);
static int hook_closedir(DIR *dir);
static FILE *hook_fopen(const char *path, const char *mode);
static size_t hook_fread(void *buf, size_t size, size_t n, FILE *file);
static size_t hook_fwrite(const void *buf, size_t size, size_t n, FILE *file);
static int hook_fclose(FILE *file);

/*
 * The ABI is the public Dobby C API at pinned revision
 * 5dfc8546954ce3b3198132ab13fddb89ee92cdd7.
 */
typedef int (*dobby_hook_fn)(void *target, void *replacement,
			     void **trampoline);
typedef int (*dobby_destroy_fn)(void *target);
static dobby_hook_fn dobby_hook;
static dobby_destroy_fn dobby_destroy;

struct io_hook_spec {
	void *target;
	void *replacement;
	void **trampoline;
	const char *name;
};

#define IO_HOOK(name) { (void *)name, (void *)hook_ ## name, \
			(void **)&orig_ ## name, #name }

static struct io_hook_spec io_hooks[] = {
	IO_HOOK(open), IO_HOOK(read), IO_HOOK(write), IO_HOOK(close),
	IO_HOOK(stat), IO_HOOK(lstat), IO_HOOK(fstat),
	IO_HOOK(opendir), IO_HOOK(readdir), IO_HOOK(closedir),
	IO_HOOK(fopen), IO_HOOK(fread), IO_HOOK(fwrite), IO_HOOK(fclose)
};
#undef IO_HOOK


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
  io_trace_in_logger++;
  fwrite(&record, 1, sizeof(record), io_trace_file);
  io_trace_in_logger--;
}

static void io_trace_shutdown(void)
{
  FILE *file = io_trace_file;
  int saved_errno = errno;
  int failed;

  if (!file)
    return;

  io_trace_active = 0;
  io_trace_single(IO_PROCESS_EXIT);
  io_trace_file = NULL;
  io_trace_in_logger++;
  failed = fflush(file) != 0 || ferror(file);
  if (fclose(file) || failed)
    fputs("git: binary I/O trace output was incomplete\n", stderr);
  io_trace_in_logger--;
  errno = saved_errno;
}


static int io_trace_load_dobby(void)
{
	const char *path = getenv("GIT_IO_TRACE_DOBBY");
#ifdef GIT_WINDOWS_NATIVE
	HMODULE dll;
#else
	void *dll;
#endif
	if (!path || !*path) {
		errno = EINVAL;
		return -1;
	}
#ifdef GIT_WINDOWS_NATIVE
	/* Refuse the implicit Windows DLL search path. */
	if (!(isalpha((unsigned char)path[0]) && path[1] == ':' &&
	      (path[2] == '\\' || path[2] == '/')) &&
	    !(path[0] == '\\' && path[1] == '\\')) {
		errno = EINVAL;
		return -1;
	}
	dll = LoadLibraryA(path);
	if (!dll) {
		errno = ENOENT;
		return -1;
	}
	dobby_hook = (dobby_hook_fn)GetProcAddress(dll, "DobbyHook");
	dobby_destroy = (dobby_destroy_fn)GetProcAddress(dll, "DobbyDestroy");
#else
	if (path[0] != '/') {
		errno = EINVAL;
		return -1;
	}
	dll = dlopen(path, RTLD_NOW | RTLD_LOCAL);
	if (!dll) {
		errno = ENOENT;
		return -1;
	}
	dobby_hook = (dobby_hook_fn)dlsym(dll, "DobbyHook");
	dobby_destroy = (dobby_destroy_fn)dlsym(dll, "DobbyDestroy");
#endif
	if (!dobby_hook || !dobby_destroy) {
		errno = ENOSYS;
		return -1;
	}
	/* The library remains loaded because patched code refers to it. */
	return 0;
}

static int io_trace_install_hooks(void)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(io_hooks); i++) {
		if (!io_hooks[i].target || !io_hooks[i].replacement ||
		    dobby_hook(io_hooks[i].target, io_hooks[i].replacement,
			       io_hooks[i].trampoline)) {
			fprintf(stderr,
				"git: DobbyHook failed for operation %s\n",
				io_hooks[i].name);
			while (i) {
				i--;
				dobby_destroy(io_hooks[i].target);
			}
			errno = ENOSYS;
			return -1;
		}
	}
	return 0;
}

int io_trace_begin(const char *option, const char *value)
{
	struct io_trace_header header = {
		{ 'G', 'I', 'T', 'I', 'O', '0', '0', '1' },
		1, 0x01020304, sizeof(struct io_trace_record), 0, 0, 0
	};
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
			if (!isxdigit(c)) {
				errno = EINVAL;
				return -1;
			}
			name[4 + i] = tolower(c);
		}
		memcpy(name, "git-", 4);
		memcpy(name + 20, ".log", 5);
		filename = name;
	} else if (strcmp(option, "--log-fn")) {
		errno = EINVAL;
		return -1;
	}

	/* The native library must be available; never silently omit hooks. */
	if (io_trace_load_dobby())
		return -1;
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
		unlink(filename);
		errno = saved_errno;
		return -1;
	}
	header.tick_frequency = io_trace_frequency();
	header.pid = (uint32_t)getpid();
	if (!header.tick_frequency ||
	    fwrite(&header, 1, sizeof(header), file) != sizeof(header)) {
		int saved_errno = errno ? errno : EIO;
		fclose(file);
		unlink(filename);
		errno = saved_errno;
		return -1;
	}
	io_trace_file = file;
	io_trace_single(IO_PROCESS_ENTER);
	if (io_trace_install_hooks()) {
		int saved_errno = errno;
		io_trace_file = NULL;
		fclose(file);
		unlink(filename);
		errno = saved_errno;
		return -1;
	}
	if (atexit(io_trace_shutdown)) {
		io_trace_active = 0;
		/* No further file operations may be traced during rollback. */
		for (i = ARRAY_SIZE(io_hooks); i; )
			dobby_destroy(io_hooks[--i].target);
		io_trace_file = NULL;
		fclose(file);
		unlink(filename);
		errno = ENOMEM;
		return -1;
	}
	io_trace_active = 1;
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
  io_trace_in_logger++;
  fwrite(pair, 1, sizeof(pair->event) + len, io_trace_file);
  io_trace_in_logger--;
}

static int hook_open(const char *path, int flags, ...)
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
  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_open(path, flags, mode);

  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_OPEN_ENTER, path, 0, flags);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_open(path, flags, mode);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       result >= 0 ? (uint64_t)result : 0, NULL);
  errno = saved_errno;
  return result;
}

static ssize_t hook_read(int fd, void *buf, size_t size)
{
  struct io_trace_pair pair;
  ssize_t result;
  int saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_read(fd, buf, size);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_READ_ENTER, NULL, fd, size);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_read(fd, buf, size);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       fd, NULL);
  errno = saved_errno;
  return result;
}

static ssize_t hook_write(int fd, const void *buf, size_t size)
{
  struct io_trace_pair pair;
  ssize_t result;
  int saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_write(fd, buf, size);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_WRITE_ENTER, NULL, fd, size);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_write(fd, buf, size);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       fd, NULL);
  errno = saved_errno;
  return result;
}

static int hook_close(int fd)
{
  struct io_trace_pair pair;
  int result, saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_close(fd);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_CLOSE_ENTER, NULL, fd, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_close(fd);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       fd, NULL);
  errno = saved_errno;
  return result;
}

static int hook_stat(const char *path, struct stat *result)
{
	struct io_trace_pair pair;
	int ret, saved_errno;

	if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
		return orig_stat(path, result);
	io_trace_call_depth++;
	io_trace_prepare(&pair, IO_STAT_ENTER, path, 0, 0);
	pair.event[0].ticks = io_trace_ticks();
	ret = orig_stat(path, result);
	saved_errno = errno;
	pair.event[1].ticks = io_trace_ticks();
	io_trace_call_depth--;
	io_trace_record_pair(&pair, ret, ret < 0 ? saved_errno : 0, 0, NULL);
	errno = saved_errno;
	return ret;
}

static int hook_lstat(const char *path, struct stat *result)
{
	struct io_trace_pair pair;
	int ret, saved_errno;

	if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
		return orig_lstat(path, result);
	io_trace_call_depth++;
	io_trace_prepare(&pair, IO_LSTAT_ENTER, path, 0, 0);
	pair.event[0].ticks = io_trace_ticks();
	ret = orig_lstat(path, result);
	saved_errno = errno;
	pair.event[1].ticks = io_trace_ticks();
	io_trace_call_depth--;
	io_trace_record_pair(&pair, ret, ret < 0 ? saved_errno : 0, 0, NULL);
	errno = saved_errno;
	return ret;
}

static int hook_fstat(int fd, struct stat *result)
{
  struct io_trace_pair pair;
  int ret, saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_fstat(fd, result);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_FSTAT_ENTER, NULL, fd, 0);
  pair.event[0].ticks = io_trace_ticks();
  ret = orig_fstat(fd, result);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, ret, ret < 0 ? saved_errno : 0, fd, NULL);
  errno = saved_errno;
  return ret;
}

static DIR *hook_opendir(const char *path)
{
  struct io_trace_pair pair;
  DIR *result;
  int saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_opendir(path);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_OPENDIR_ENTER, path, 0, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_opendir(path);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, (intptr_t)result,
                       !result ? saved_errno : 0,
                       (uintptr_t)result, NULL);
  errno = saved_errno;
  return result;
}

static struct dirent *hook_readdir(DIR *dir)
{
  struct io_trace_pair pair;
  struct dirent *result;
  int saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_readdir(dir);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_READDIR_ENTER, NULL, (uintptr_t)dir, 0);
  pair.event[0].ticks = io_trace_ticks();
  errno = 0;
  errno = 0;
  result = orig_readdir(dir);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, (intptr_t)result,
                       !result ? saved_errno : 0,
                       (uintptr_t)dir, result ? result->d_name : NULL);
  errno = saved_errno;
  return result;
}

static int hook_closedir(DIR *dir)
{
  struct io_trace_pair pair;
  int result, saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_closedir(dir);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_CLOSEDIR_ENTER, NULL, (uintptr_t)dir, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_closedir(dir);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       (uintptr_t)dir, NULL);
  errno = saved_errno;
  return result;
}

static FILE *hook_fopen(const char *path, const char *mode)
{
  struct io_trace_pair pair;
  FILE *result;
  int saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_fopen(path, mode);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_FOPEN_ENTER, path, 0, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_fopen(path, mode);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, (intptr_t)result,
                       !result ? saved_errno : 0,
                       (uintptr_t)result, NULL);
  errno = saved_errno;
  return result;
}

static size_t hook_fread(void *ptr, size_t size, size_t count, FILE *stream)
{
  struct io_trace_pair pair;
  size_t result;
  int saved_errno, error;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_fread(ptr, size, count, stream);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_FREAD_ENTER, NULL, (uintptr_t)stream,
                   size && count <= SIZE_MAX / size ? size * count : SIZE_MAX);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_fread(ptr, size, count, stream);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  error = result != count && ferror(stream) ? saved_errno : 0;
  io_trace_record_pair(&pair,
                       size && result <= SIZE_MAX / size ? result * size : SIZE_MAX,
                       error,
                       (uintptr_t)stream, NULL);
  errno = saved_errno;
  return result;
}

static size_t hook_fwrite(const void *ptr, size_t size, size_t count, FILE *stream)
{
  struct io_trace_pair pair;
  size_t result;
  int saved_errno, error;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_fwrite(ptr, size, count, stream);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_FWRITE_ENTER, NULL, (uintptr_t)stream,
                   size && count <= SIZE_MAX / size ? size * count : SIZE_MAX);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_fwrite(ptr, size, count, stream);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  error = result != count && ferror(stream) ? saved_errno : 0;
  io_trace_record_pair(&pair, size && result <= SIZE_MAX / size ? result * size : SIZE_MAX, error,
                       (uintptr_t)stream, NULL);
  errno = saved_errno;
  return result;
}

static int hook_fclose(FILE *stream)
{
  struct io_trace_pair pair;
  int result, saved_errno;

  if (!io_trace_active || io_trace_in_logger || io_trace_call_depth)
    return orig_fclose(stream);
  io_trace_call_depth++;
  io_trace_prepare(&pair, IO_FCLOSE_ENTER, NULL, (uintptr_t)stream, 0);
  pair.event[0].ticks = io_trace_ticks();
  result = orig_fclose(stream);
  saved_errno = errno;
  pair.event[1].ticks = io_trace_ticks();
  io_trace_call_depth--;
  io_trace_record_pair(&pair, result, result < 0 ? saved_errno : 0,
                       (uintptr_t)stream, NULL);
  errno = saved_errno;
  return result;
}
