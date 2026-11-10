#ifndef PRELOAD_WORKER_H
#define PRELOAD_WORKER_H

/**
 * @file
 * One-command preload workers. Startup dispatch calls preload_worker_main();
 * common_exit() calls preload_worker_on_exit(). A worker owns its transport
 * until command completion; an ordinary Git process has no active worker.
 */
struct trace_key;
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
 * @brief Complete platform output flushing after the C stream has been flushed.
 * @param fd Borrowed writable standard-output descriptor, still open.
 * @return Zero on success, -1 on failure, with errno set.
 */
int preload_worker_flush_output(int fd);
/**
 * @brief Prepare backend endpoints and owner monitoring before READY.
 * @param directory Borrowed private worker directory, valid through process exit.
 * @param location Receives an owned endpoint stem on success; valid until transport release.
 * @return Zero on success, nonzero startup status on failure.
 */
int preload_worker_platform_prepare(const char *directory, char **location);
/** @return Owned EOF-producing input descriptor, or -1 with errno set. */
int preload_worker_open_input(void);
/** @return Zero after installing platform standard handles, -1 on failure. */
int preload_worker_platform_stdio(void);
/**
 * @brief Remove worker-owned filesystem endpoints after transport release.
 * @param directory Borrowed private worker directory.
 */
void preload_worker_platform_cleanup(const char *directory);
/**
 * @brief Retained exit investigation diagnostic until migration acceptance.
 * @param event Borrowed event label, valid for this call.
 * @param status Exit value, or a negative value to record only the label.
 */
void preload_worker_exit_diagnostic(const char *event, int status);

#endif
