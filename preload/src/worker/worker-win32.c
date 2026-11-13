#include "git-compat-util.h"
#include "worker/worker.h"
#include "shared/transport.h"
#include "trace.h"

/* Owned process-lifetime path; owner monitoring remains active through CRT exit. */
static char *preload_worker_directory;

/**
 * Wait for the reader to consume buffered output before closing its pipe.
 * @param fd Borrowed synchronous output descriptor, still open.
 * @return Zero on success, -1 with errno set on failure.
 */
int preload_worker_flush_output(int fd)
{
	if (FlushFileBuffers((HANDLE)_get_osfhandle(fd)))
		return 0;
	errno = err_win_to_posix(GetLastError());
	return -1;
}

/*
 * Git Bash can open a throwaway pipe instance during redirection
 * (see https://github.com/Ma-XX-oN/git/issues/7).
 * Two threads, created before READY, traverse all five channels themselves.
 * They connect and read one marker each; pipes remain synchronous for Git I/O.
 */
/*
 * The manager transfers each worker to the client at allocation time. Keep an
 * independent reference to the owning application so an abandoned worker
 * terminates even if it is blocked on a named pipe.
 *
 * Waiting on the process handle is stronger than polling its PID: it wakes
 * when the original process exits and cannot confuse a reused numeric PID.
 */
/**
 * @brief Terminate an abandoned worker when the captured owner handle signals.
 *
 * @param context Owned synchronous owner process handle, retained until process exit.
 * @return Never returns; owner death terminates the worker with status 137.
 */
static DWORD WINAPI preload_worker_watch_owner(LPVOID context)
{
	HANDLE owner = (HANDLE)context;
	WaitForSingleObject(owner, INFINITE);
	trace_printf_key(&preload_worker_trace, "owner died\n");
	rmdir(preload_worker_directory);
	ExitProcess(137);
	return 0;
}

/**
 * @brief Capture the original application process and start its lifetime watcher.
 *
 * @return Zero on success, -1 for an invalid/dead owner or native startup failure.
 */
static int preload_worker_start_owner_watch(void)
{
	char value[32];
	DWORD count, pid = 0, i;
	HANDLE owner, thread;

	count = GetEnvironmentVariableA("PRELOAD_PID", value, sizeof(value));
	if (!count || count >= sizeof(value))
		return -1;
	for (i = 0; i < count; i++) {
		unsigned char c = value[i];
		if (c < '0' || c > '9' ||
		    pid > (0xffffffffUL - (c - '0')) / 10)
			return -1;
		pid = pid * 10 + c - '0';
	}
	if (!pid)
		return -1;
	owner = OpenProcess(SYNCHRONIZE, FALSE, pid);
	if (!owner)
		return -1;
	if (WaitForSingleObject(owner, 0) != WAIT_TIMEOUT) {
		CloseHandle(owner);
		return -1;
	}
	thread = CreateThread(NULL, 0, preload_worker_watch_owner, owner, 0, NULL);
	if (!thread) {
		CloseHandle(owner);
		return -1;
	}
	CloseHandle(thread);
	/* The watcher owns the process handle until this worker exits. */
	return 0;
}


/**
 * @brief Prepare owner monitoring and the native endpoint name.
 *
 * @param directory Borrowed existing private worker directory.
 * @param location Receives an owned native pipe "directory" on success:
 *        PRELOAD_WIN_DIR followed by \worker-<pid>.
 * @return Zero on success, or a worker startup status on failure.
 */
int preload_worker_platform_prepare(const char *directory, char **location)
{
	const char *pipes = getenv("PRELOAD_WIN_DIR");

	preload_worker_directory = xstrdup(directory);
	if (!pipes || !*pipes) {
		error("--preload-worker requires PRELOAD_WIN_DIR");
		return 136;
	}
	if (preload_worker_start_owner_watch()) {
		return 136;
	}
	*location = xstrfmt("%s\\worker-%lu", pipes, (unsigned long)getpid());
	return 0;
}

/**
 * @brief Write the stdin ACK byte back to the client on its duplex pipe.
 *
 * Every call marker has been read when the transport finishes connecting, so
 * the client may then close stdin (end-of-file) or write input safely. The
 * byte is buffered, not flushed, so this never waits for the client.
 *
 * @param transport Borrowed connected transport, still owning the channel.
 * @param channel Configured index of the stdin channel.
 * @return Zero on success, -1 with errno set on failure.
 */
int preload_worker_acknowledge_stdin(struct preload_transport *transport, size_t channel)
{
	static const char ack = 0x06;

	return preload_transport_write(transport, channel, &ack, 1) == 1 ? 0 : -1;
}

/**
 * @brief Synchronize Win32 standard handles after CRT descriptor replacement.
 *
 * @return Zero on success, -1 with errno set on failure.
 */
int preload_worker_platform_stdio(void)
{
	if (SetStdHandle(STD_INPUT_HANDLE, (HANDLE)_get_osfhandle(0)) &&
	    SetStdHandle(STD_OUTPUT_HANDLE, (HANDLE)_get_osfhandle(1)) &&
	    SetStdHandle(STD_ERROR_HANDLE, (HANDLE)_get_osfhandle(2)))
		return 0;
	errno = err_win_to_posix(GetLastError());
	return -1;
}

/**
 * @brief Remove a finished worker's empty private directory.
 *
 * @param directory Borrowed directory created by shared worker startup.
 */
void preload_worker_platform_cleanup(const char *directory)
{
	if (rmdir(directory) && errno != ENOENT)
		trace_printf_key(&preload_worker_trace, "worker directory cleanup failed: %s errno=%d\n",
				 directory, errno);
}
