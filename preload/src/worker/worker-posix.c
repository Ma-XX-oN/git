#include "git-compat-util.h"
#include "worker/worker.h"
#include "trace.h"
#include <fcntl.h>
#include <limits.h>
#ifndef NO_PTHREADS
#include <pthread.h>
#endif
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * A --preload-worker worker executes one command and exits. Its private channel
 * directory is created by shared worker startup before FIFO preparation.
 */
/**
 * @brief Validate the output descriptor after its C stream has been flushed.
 *
 * @param fd Borrowed open output descriptor; FIFO writes already reach the kernel.
 * @return Zero for a valid descriptor, -1 with errno set otherwise.
 */
int preload_worker_flush_output(int fd)
{
	return fcntl(fd, F_GETFD) < 0 ? -1 : 0;
}

/*
 * The allocation manager relinquishes command ownership to the client. Check
 * the originating application's PID independently every 10 seconds. Do not use
 * getppid(): the worker's immediate parent is the allocation manager.
 */
/**
 * @brief Remove this single-use worker's private FIFO endpoints.
 *
 * @param directory Borrowed worker-owned directory; absent endpoints are safe.
 */
static void preload_worker_remove_fifos(const char *directory)
{
	static const char *const names[5] = {
		"stdout", "stderr", "exit-code", "stdin", "params-cwd"
	};
	size_t i;

	for (i = 0; i < ARRAY_SIZE(names); i++) {
		char *path = xstrfmt("%s/%s", directory, names[i]);

		if (unlink(path) && errno != ENOENT)
			trace_printf_key(&preload_worker_trace,
					 "FIFO cleanup failed: %s errno=%d\n", path, errno);
		free(path);
	}
	if (rmdir(directory) && errno != ENOENT)
		trace_printf_key(&preload_worker_trace,
				 "worker directory cleanup failed: %s errno=%d\n", directory, errno);
}

#ifndef NO_PTHREADS
/** Owned immutable state retained by the owner-monitor thread until process exit. */
struct preload_owner_watch {
	pid_t pid; /**< Captured native owner PID, independent of later environment changes. */
	char directory[FLEX_ARRAY]; /**< Owned copy of the worker endpoint directory. */
};

/**
 * @brief Prepare the FIFO channels before publishing READY.
 *
 * @param directory Borrowed newly created private worker directory.
 * @return Zero on success, -1 with errno set on failure; caller removes partial FIFOs.
 */
static int preload_worker_create_fifos(const char *directory)
{
	static const char *const names[] = {
		"stdout", "stderr", "exit-code", "stdin", "params-cwd"
	};
	size_t i;

	for (i = 0; i < ARRAY_SIZE(names); i++) {
		char *path = xstrfmt("%s/%s", directory, names[i]);
		int result = mkfifo(path, 0600);

		free(path);
		if (result)
			return -1;
	}
	return 0;
}

/**
 * @brief Terminate and remove endpoints when the original application owner dies.
 *
 * @param context Owned preload_owner_watch, retained until process termination.
 * @return Never returns; owner death terminates the worker process with status 137.
 */
static void *preload_worker_watch_owner(void *context)
{
	struct preload_owner_watch *watch = context;
	pid_t owner_pid = watch->pid;

	for (;;) {
		unsigned int left = 10;

		while (left)
			left = sleep(left);
		if (kill(owner_pid, 0) < 0 && errno == ESRCH) {
			trace_printf_key(&preload_worker_trace,
					 "owner died pid=%ld\n", (long)owner_pid);
			preload_worker_remove_fifos(watch->directory);
			_exit(137);
		}
	}
}

/**
 * @brief Capture the owner PID and keep its watcher alive until process exit.
 *
 * @param directory Borrowed private worker directory; copied into monitor-owned state.
 * @return Zero on success, -1 for invalid/dead owner or thread startup failure.
 */
static int preload_worker_start_owner_watch(const char *directory)
{
	const char *raw = getenv("PRELOAD_PID");
	unsigned long value = 0;
	pthread_t watcher;
	struct preload_owner_watch *watch;
	size_t i;

	if (!raw || !*raw)
		return -1;
	for (i = 0; raw[i]; i++) {
		unsigned char ch = raw[i];

		if (ch < '0' || ch > '9' ||
		    value > (unsigned long)(LONG_MAX - (ch - '0')) / 10)
			return -1;
		value = value * 10 + ch - '0';
	}
	if (!value || value > INT_MAX ||
	    (kill((pid_t)value, 0) < 0 && errno == ESRCH))
		return -1;
	FLEX_ALLOC_STR(watch, directory, directory);
	watch->pid = (pid_t)value;
	if (pthread_create(&watcher, NULL, preload_worker_watch_owner, watch)) {
		free(watch);
		return -1;
	}
	pthread_detach(watcher);
	return 0;
}
#endif

/**
 * @brief Prepare FIFO endpoints and owner monitoring before READY.
 *
 * @param directory Borrowed existing private worker directory.
 * @param location Receives an owned FIFO-directory string on success.
 * @return Zero on success, or a worker startup status on failure.
 */
int preload_worker_platform_prepare(const char *directory, char **location)
{
#ifdef NO_PTHREADS
	*location = NULL;
	error("--preload-worker requires POSIX thread support");
	trace_printf_key(&preload_worker_trace,
			 "worker startup requires POSIX thread support directory=%s\n", directory);
	return 136;
#else
	if (preload_worker_create_fifos(directory))
		return 121;
	if (preload_worker_start_owner_watch(directory))
		return 136;
	*location = xstrdup(directory);
	return 0;
#endif
}

/** @return Owned EOF-producing input descriptor, or -1 with errno set. */
int preload_worker_open_input(void)
{
	return open("/dev/null", O_RDONLY);
}

/**
 * @brief POSIX standard descriptors are already installed by dup2.
 *
 * @return Zero; no separate operating-system handle table requires updating.
 */
int preload_worker_platform_stdio(void)
{
	return 0;
}

/**
 * @brief Remove this worker's FIFOs and private directory.
 *
 * @param directory Borrowed directory created by shared worker startup.
 */
void preload_worker_platform_cleanup(const char *directory)
{
	preload_worker_remove_fifos(directory);
}
