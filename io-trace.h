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

/* Enabled only in the opt-in investigation build. No macros are installed. */
int io_trace_begin(const char *option, const char *value);

#endif
