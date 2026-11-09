#include "git-compat-util.h"
#include "common-init.h"
#include "preload-context.h"
#include "preload-worker.h"
#include "preload-transport.h"
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <pthread.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * A --preload-worker worker executes one command and exits. Its private channel
 * directory is created by the owning manager, not by the Git worker.
 */
static jmp_buf preload_exit_jump;
static volatile int preload_exit_active;
static volatile int preload_exit_status;

/** @brief Return command exits to the worker's ordered completion path. */
NORETURN static void preload_worker_finish(int status)
{
	if (preload_exit_active) {
		preload_exit_status = status;
		longjmp(preload_exit_jump, 1);
	}
	exit(status);
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

/** @brief Accept one FIFO request and complete output before publishing status. */
int preload_worker_run(const char *directory)
{
	static const struct preload_channel_spec channels[] = {
		{ "stdout", 1 }, { "stderr", 1 }, { "exit-code", 1 }, { "stdin", 0 }, { "param-env-cwd", 0 }
	};
	static const size_t order[] = { 0, 1, 2, 3, 4 };
	struct preload_transport_config config = { 0 };
	struct preload_transport *transport = NULL;
	char *frame = NULL;
	char result[64];
	const size_t frame_capacity = 131072;
	const char *args[33] = { NULL };
	int fd[5] = { -1, -1, -1, -1, -1 };
	int i, count, pos, rc = 120;
	size_t used = 0;
	ssize_t n;

	if (preload_worker_start_owner_watch(directory))
		return 136;
	if (!directory || strlen(directory) > 960)
		return 121;
	frame = malloc(frame_capacity);
	if (!frame)
		return 121;
	config.location = directory;
	config.channels = channels;
	config.order = order;
	config.nr = ARRAY_SIZE(channels);
	if (preload_transport_start(&transport, &config)) {
		rc = 121;
		goto cleanup;
	}
	printf("READY %ld\n", (long)getpid());
	fflush(stdout);
	/* Match the four-channel open order used by git-ipc. */
	if (preload_transport_wait(transport) != PRELOAD_TRANSPORT_OK) {
		rc = 122;
		goto cleanup;
	}
	while (used < frame_capacity) {
		n = preload_transport_read(transport, 4, frame + used, frame_capacity - used);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			rc = 124;
			goto cleanup;
		}
		if (!n)
			break;
		used += (size_t)n;
	}
	preload_transport_close_channel(transport, 4);
	if (used < 4 || used == frame_capacity) {
		rc = 125;
		goto cleanup;
	}
	count = (unsigned char)frame[0] |
		((unsigned char)frame[1] << 8) |
		((unsigned char)frame[2] << 16) |
		((unsigned char)frame[3] << 24);
	if (count < 1 || count > 32) {
		rc = 126;
		goto cleanup;
	}
	pos = 4;
	for (i = 0; i < count; i++) {
		int start = pos;
		if ((size_t)pos >= used) {
			rc = 127;
			goto cleanup;
		}
		while ((size_t)pos < used && frame[pos])
			pos++;
		if ((size_t)pos == used) {
			rc = 128;
			goto cleanup;
		}
		args[i] = frame + start;
		pos++;
	}
	if (strcmp(args[0], "git") ||
	    preload_context_apply(frame + pos, used - (size_t)pos)) {
		rc = 129;
		goto cleanup;
	}
	args[count] = NULL;

	fd[3] = preload_transport_detach_fd(transport, 3);
	fd[0] = preload_transport_detach_fd(transport, 0);
	fd[1] = preload_transport_detach_fd(transport, 1);
	/*
	 * The request writer has reached EOF, but its readable endpoint remains
	 * valid. Redirect it to Git's stdin instead of closing descriptor zero.
	 */
	if (dup2(fd[3], STDIN_FILENO) < 0 ||
	    dup2(fd[0], STDOUT_FILENO) < 0 ||
	    dup2(fd[1], STDERR_FILENO) < 0) {
		rc = 130;
		goto cleanup;
	}
	close(fd[3]);
	fd[3] = -1;
	close(fd[0]);
	fd[0] = -1;
	close(fd[1]);
	fd[1] = -1;

	/* Defer Git initialization until after the command arrives. */
	preload_exit_active = 1;
	preload_worker_set_exit_handler(preload_worker_finish);
	if (!setjmp(preload_exit_jump)) {
		init_git(args);
		rc = cmd_main(count, args);
		preload_worker_set_exit_handler(NULL);
		rc = common_exit(__FILE__, __LINE__, rc);
	} else {
		rc = preload_exit_status;
	}
	preload_worker_set_exit_handler(NULL);
	preload_exit_active = 0;

	fflush(stdout);
	fflush(stderr);
	close(STDOUT_FILENO);
	close(STDERR_FILENO);
	close(STDIN_FILENO);

cleanup:
	/* Deliver output EOF before status even when request validation fails. */
	for (i = 0; i < 5; i++)
		if (fd[i] >= 0)
			close(fd[i]);
	if (transport) {
		for (i = 0; i < 5; i++)
			if (i != 2)
				preload_transport_close_channel(transport, i);
		n = snprintf(result, sizeof(result), "EXIT %d\n", rc);
		if (n > 0 && (size_t)n < sizeof(result))
			preload_transport_write(transport, 2, result, (size_t)n);
		preload_transport_release(transport);
	}
	preload_worker_remove_fifos(directory);
	free(frame);
	return rc;
}
