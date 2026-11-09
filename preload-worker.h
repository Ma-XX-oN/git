#ifndef PRELOAD_WORKER_H
#define PRELOAD_WORKER_H

#include <stddef.h>

/**
 * @file
 * One-command preload workers. Startup dispatch calls preload_worker_main();
 * common_exit() calls preload_worker_on_exit(). A worker owns its transport
 * until command completion; an ordinary Git process has no registered handler.
 */
struct trace_key;
struct preload_transport;
extern struct trace_key preload_worker_trace;

/**
 * @brief Validate worker startup arguments and run the platform worker.
 * @param argc Number of argv entries, including git and --preload-worker.
 * @param argv Borrowed startup arguments, including an existing session directory.
 * @return Startup error status; command execution exits the process normally.
 */
int preload_worker_main(int argc, const char **argv);
/**
 * @brief Complete output and status for this process's active command.
 * @param status Git command exit value; ordinary Git processes have no active worker.
 */
void preload_worker_on_exit(int status);
/**
 * Complete platform output flushing after the C stream has been flushed.
 * @param fd Borrowed writable standard-output descriptor, still open.
 * @return Zero on success, -1 on failure, with errno set.
 */
int preload_worker_flush_output(int fd);
/**
 * Prepare backend endpoints and owner monitoring before READY.
 * @param directory Borrowed private worker directory, valid through process exit.
 * @return Zero on success, nonzero startup status on failure.
 */
int preload_worker_platform_prepare(const char *directory);
/**
 * Resolve the transport's location from the shared session identity.
 * @param directory Borrowed private worker directory.
 * @return Newly allocated location, owned by the caller until transport release.
 */
char *preload_worker_platform_location(const char *directory);
/** @return Owned EOF-producing input descriptor, or -1 with errno set. */
int preload_worker_open_input(void);
/** @return Zero after installing platform standard handles, -1 on failure. */
int preload_worker_platform_stdio(void);
/**
 * Remove worker-owned filesystem endpoints after transport release.
 * @param directory Borrowed private worker directory.
 */
void preload_worker_platform_cleanup(const char *directory);
/** @brief Retained exit investigation diagnostic until migration acceptance. */
void preload_worker_exit_diagnostic(const char *event, int status);

#endif
