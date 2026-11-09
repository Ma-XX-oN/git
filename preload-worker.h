#ifndef PRELOAD_WORKER_H
#define PRELOAD_WORKER_H

/**
 * @file
 * One-command preload workers. Startup dispatch calls preload_worker_main();
 * common_exit() calls preload_worker_on_exit(). A worker owns its transport
 * until command completion; an ordinary Git process has no registered handler.
 */
struct trace_key;
extern struct trace_key preload_worker_trace;

/** @brief Validate worker startup arguments and run the platform worker. */
int preload_worker_main(int argc, const char **argv);
/** @brief Run completion registered by this worker, if one is active. */
void preload_worker_on_exit(int status);
/** @brief Register or clear this process's single worker completion handler. */
void preload_worker_set_exit_handler(void (*handler)(int));
/** @brief Platform implementation; directory is required only on POSIX. */
int preload_worker_run(const char *directory);
/** @brief Retained exit investigation diagnostic until migration acceptance. */
void preload_worker_exit_diagnostic(const char *event, int status);

#endif
