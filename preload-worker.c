#include "git-compat-util.h"
#include "preload-worker.h"
#include "trace.h"

struct trace_key preload_worker_trace = TRACE_KEY_INIT(PRELOAD);
static void (*worker_exit_handler)(int);

/** @brief Register or clear completion for the one command in this process. */
void preload_worker_set_exit_handler(void (*handler)(int))
{
	worker_exit_handler = handler;
}

/** @brief Preserve normal Git exit behavior outside an active preload worker. */
void preload_worker_on_exit(int status)
{
	if (worker_exit_handler)
		worker_exit_handler(status);
}

/** @brief Dispatch the exclusive startup mode without initializing a repository. */
int preload_worker_main(int argc, const char **argv)
{
#ifdef GIT_WINDOWS_NATIVE
	if (argc != 2) {
		fprintf(stderr, "error: --preload-worker must be used without other arguments\n");
		return 129;
	}
	return preload_worker_run(NULL);
#else
	if (argc != 3) {
		fprintf(stderr, "error: --preload-worker requires a worker directory\n");
		return 129;
	}
	return preload_worker_run(argv[2]);
#endif
}

#ifndef GIT_WINDOWS_NATIVE
/** @brief POSIX workers do not use the retained Windows exit-file diagnostic. */
void preload_worker_exit_diagnostic(const char *event UNUSED, int status UNUSED)
{
}
#endif
