#ifndef IO_TRACE_H
#define IO_TRACE_H

/*
 * Optional, investigation-only binary I/O instrumentation.
 * Enable with "make GIT_IO_TRACE=1"; disabled builds are unchanged.
 * The file format is described in Documentation/technical/io-trace.adoc.
 */
enum io_trace_event_type {
  IO_PROCESS_ENTER = 0,
  IO_PROCESS_EXIT = 1,
  IO_OPEN_ENTER = 2,
  IO_OPEN_EXIT = 3,
  IO_READ_ENTER = 4,
  IO_READ_EXIT = 5,
  IO_WRITE_ENTER = 6,
  IO_WRITE_EXIT = 7,
  IO_CLOSE_ENTER = 8,
  IO_CLOSE_EXIT = 9,
  IO_STAT_ENTER = 10,
  IO_STAT_EXIT = 11,
  IO_LSTAT_ENTER = 12,
  IO_LSTAT_EXIT = 13,
  IO_FSTAT_ENTER = 14,
  IO_FSTAT_EXIT = 15,
  IO_OPENDIR_ENTER = 16,
  IO_OPENDIR_EXIT = 17,
  IO_READDIR_ENTER = 18,
  IO_READDIR_EXIT = 19,
  IO_CLOSEDIR_ENTER = 20,
  IO_CLOSEDIR_EXIT = 21,
  IO_FOPEN_ENTER = 22,
  IO_FOPEN_EXIT = 23,
  IO_FREAD_ENTER = 24,
  IO_FREAD_EXIT = 25,
  IO_FWRITE_ENTER = 26,
  IO_FWRITE_EXIT = 27,
  IO_FCLOSE_ENTER = 28,
  IO_FCLOSE_EXIT = 29
};

int io_trace_begin(const char *option, const char *value);
int io_trace_open(const char *path, int flags, ...);
ssize_t io_trace_read(int fd, void *buf, size_t size);
ssize_t io_trace_write(int fd, const void *buf, size_t size);
int io_trace_close(int fd);
int io_trace_stat(const char *path, struct stat *result);
int io_trace_lstat(const char *path, struct stat *result);
int io_trace_fstat(int fd, struct stat *result);
DIR *io_trace_opendir(const char *path);
struct dirent *io_trace_readdir(DIR *dir);
int io_trace_closedir(DIR *dir);
FILE *io_trace_fopen(const char *path, const char *mode);
size_t io_trace_fread(void *ptr, size_t size, size_t count, FILE *stream);
size_t io_trace_fwrite(const void *ptr, size_t size, size_t count, FILE *stream);
int io_trace_fclose(FILE *stream);

#if defined(GIT_IO_TRACE) && !defined(IO_TRACE_INTERNAL)
/* After native platform remappings in compat/posix.h have been declared. */
#undef open
#undef read
#undef write
#undef close
#undef stat
#undef lstat
#undef fstat
#undef opendir
#undef readdir
#undef closedir
#undef fopen
#undef fread
#undef fwrite
#undef fclose
#define open(...) io_trace_open(__VA_ARGS__)
#define read(fd, buf, n) io_trace_read(fd, buf, n)
#define write(fd, buf, n) io_trace_write(fd, buf, n)
#define close(fd) io_trace_close(fd)
/*
 * Windows has an object-like "stat -> mingw_stat" remapping which is
 * also used for "struct stat". Retain it and intercept the called name.
 */
#ifdef GIT_WINDOWS_NATIVE
#define stat mingw_stat
#define lstat mingw_lstat
#define fstat mingw_fstat
#define mingw_stat(path, buf) io_trace_stat(path, buf)
#define mingw_lstat(path, buf) io_trace_lstat(path, buf)
#define mingw_fstat(fd, buf) io_trace_fstat(fd, buf)
#else
#define stat(path, buf) io_trace_stat(path, buf)
#define lstat(path, buf) io_trace_lstat(path, buf)
#define fstat(fd, buf) io_trace_fstat(fd, buf)
#endif
#define opendir(path) io_trace_opendir(path)
#define readdir(dir) io_trace_readdir(dir)
#define closedir(dir) io_trace_closedir(dir)
#define fopen(path, mode) io_trace_fopen(path, mode)
#define fread(ptr, size, n, stream) io_trace_fread(ptr, size, n, stream)
#define fwrite(ptr, size, n, stream) io_trace_fwrite(ptr, size, n, stream)
#define fclose(stream) io_trace_fclose(stream)
#endif

#endif
