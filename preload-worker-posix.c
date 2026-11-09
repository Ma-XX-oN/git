#include "git-compat-util.h"
#include "common-init.h"
#include "preload-context.h"
#include "preload-worker.h"
#include "preload-transport.h"
#include "trace.h"
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <pthread.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * A --preload-worker worker executes one command and exits. Its private channel
 * directory is created by the owning manager, not by the Git worker.
 */
/** @param fd Borrowed output descriptor; FIFO writes already reach the kernel. */
int preload_worker_flush_output(int fd)
{
	return fcntl(fd, F_GETFD) < 0 ? -1 : 0;
}

/*
 * The allocation manager relinquishes command ownership to git-ipc. Check
 * the originating Bash PID independently every 10 seconds. Do not use
 * getppid(): the worker's immediate parent is the allocation manager.
 */
/** @brief Remove this single-use worker's private FIFO endpoints. */
static void preload_worker_remove_fifos(const char *directory)
{
	static const char *const names[5] = {
		"stdout", "stderr", "exit-code", "stdin", "param-env-cwd"
	};
	char path[1024];
	int i;

	for (i = 0; i < 5; i++)
		if (snprintf(path, sizeof(path), "%s/%s", directory, names[i]) <
		    sizeof(path))
			unlink(path);
	rmdir(directory);
}

struct preload_owner_watch {
	pid_t pid;
	char directory[1];
};

/**
 * @brief Prepare the FIFO channels before publishing READY.
 * @param directory Borrowed newly created private worker directory.
 * @return Zero on success, -1 with errno set on failure; caller removes partial FIFOs.
 */
static int preload_worker_create_fifos(const char *directory)
{
	static const char *const names[] = {
		"stdout", "stderr", "exit-code", "stdin", "param-env-cwd"
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

/** @brief Terminate and remove endpoints when the original shell owner dies. */
static void *preload_worker_watch_owner(void *context)
{
	struct preload_owner_watch *watch = context;
	pid_t owner_pid = watch->pid;

	for (;;) {
		unsigned int left = 10;

		while (left)
			left = sleep(left);
		if (kill(owner_pid, 0) < 0 && errno == ESRCH) {
			preload_worker_remove_fifos(watch->directory);
			_exit(137);
		}
	}
}

/** @brief Capture the owner PID and keep its watcher alive until process exit. */
static int preload_worker_start_owner_watch(const char *directory)
{
	const char *raw = getenv("GIT_PRELOAD_OWNER_PID");
	unsigned long value = 0;
	pthread_t watcher;
	struct preload_owner_watch *watch;
	size_t i, n;

	if (!raw || !*raw)
		return -1;
	for (i = 0; raw[i]; i++) {
		unsigned char ch = raw[i];

		if (ch < '0' || ch > '9' ||
		    value > (LONG_MAX - (ch - '0')) / 10)
			return -1;
		value = value * 10 + ch - '0';
	}
	if (!value || value > INT_MAX ||
	    (kill((pid_t)value, 0) < 0 && errno == ESRCH))
		return -1;
	n = strlen(directory);
	watch = malloc(sizeof(*watch) + n);
	if (!watch)
		return -1;
	watch->pid = (pid_t)value;
	memcpy(watch->directory, directory, n + 1);
	if (pthread_create(&watcher, NULL, preload_worker_watch_owner, watch)) {
		free(watch);
		return -1;
	}
	pthread_detach(watcher);
	return 0;
}

/**
 * @brief Prepare FIFO endpoints and owner monitoring before READY.
 * @param directory Borrowed existing private worker directory.
 * @return Zero on success, or a worker startup status on failure.
 */
int preload_worker_platform_prepare(const char *directory)
{
	if (strlen(directory) > 960 || preload_worker_create_fifos(directory))
		return 121;
	return preload_worker_start_owner_watch(directory) ? 136 : 0;
}

/**
 * @brief Use the private worker directory as the FIFO location.
 * @param directory Borrowed directory created by shared worker startup.
 * @return Newly owned location string, freed by the shared worker.
 */
char *preload_worker_platform_location(const char *directory)
{
	return xstrdup(directory);
}

/** @return Owned EOF-producing input descriptor, or -1 with errno set. */
int preload_worker_open_input(void)
{
	return open("/dev/null", O_RDONLY);
}

/**
 * @brief POSIX standard descriptors are already installed by dup2.
 * @return Zero; no separate operating-system handle table requires updating.
 */
int preload_worker_platform_stdio(void)
{
	return 0;
}

/**
 * @brief Remove this worker's FIFOs and private directory.
 * @param directory Borrowed directory created by shared worker startup.
 */
void preload_worker_platform_cleanup(const char *directory)
{
	preload_worker_remove_fifos(directory);
}
