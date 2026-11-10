#include "git-compat-util.h"
#include "preload-worker.h"
#include "trace.h"

/* Owned process-lifetime path; owner monitoring remains active through CRT exit. */
static char *preload_worker_directory;

/*
 * Snapshot the trace path before Git can modify its environment. CRT shutdown
 * can run after Git's trace descriptors are closed; do not use trace_printf_key
 * or any of its file descriptors from this atexit diagnostic.
 */
static wchar_t preload_worker_exit_log_path[4096];

/*
 * Use a new Win32 handle for every diagnostic: Git may already have closed
 * Trace2 and trace_printf_key's descriptors during shutdown.
 */
/**
 * @brief Append retained shutdown evidence without relying on trace descriptors.
 * @param event Borrowed event label, valid for this call.
 * @param status Exit value to append, or a negative value to record only the label.
 */
void preload_worker_exit_diagnostic(const char *event, int status)
{
	char message[160], error_message[200];
	HANDLE file;
	DWORD written = 0, length;
	int count;

	if (!preload_worker_exit_log_path[0])
		return;
	count = status < 0 ? snprintf(message, sizeof(message), "%s\r\n", event) :
			     snprintf(message, sizeof(message), "%s status=%d\r\n", event, status);
	if (count < 0 || count >= sizeof(message))
		return;
	length = (DWORD)count;
	file = CreateFileW(preload_worker_exit_log_path, FILE_APPEND_DATA,
			   FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
			   NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
	if (file == INVALID_HANDLE_VALUE) {
		snprintf(error_message, sizeof(error_message),
			 "preload diagnostic open failed error=%lu\n", GetLastError());
		OutputDebugStringA(error_message);
		return;
	}
	if (!WriteFile(file, message, length, &written, NULL) || written != length) {
		snprintf(error_message, sizeof(error_message),
			 "preload diagnostic write failed error=%lu written=%lu expected=%lu\n",
			 GetLastError(), written, length);
		OutputDebugStringA(error_message);
	}
	if (!CloseHandle(file))
		OutputDebugStringA("preload diagnostic close failed\n");
}

/** @brief Record entry to CRT shutdown for migration acceptance diagnostics. */
static void preload_worker_atexit_log(void)
{
	preload_worker_exit_diagnostic("exiting...", -1);
}

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
 * Git Bash can open a throwaway pipe instance during redirection.
 * Two threads, created before READY, traverse all five channels themselves.
 * They connect and read one marker each; pipes remain synchronous for Git I/O.
 */
/*
 * The manager transfers each worker to git-ipc at allocation time. Keep an
 * independent reference to the owning application so an abandoned worker
 * terminates even if it is blocked on a named pipe.
 *
 * Waiting on the process handle is stronger than polling its PID: it wakes
 * when the original process exits and cannot confuse a reused numeric PID.
 */
/**
 * @brief Terminate an abandoned worker when the captured owner handle signals.
 * @param context Owned synchronous owner process handle, retained until process exit.
 * @return Never returns; owner death terminates the worker with status 137.
 */
static DWORD WINAPI preload_worker_watch_owner(LPVOID context)
{
	HANDLE owner = (HANDLE)context;
	WaitForSingleObject(owner, INFINITE);
	trace_printf_key(&preload_worker_trace, "owner died\n");
	preload_worker_exit_diagnostic("ExitProcess owner-death", 137);
	rmdir(preload_worker_directory);
	ExitProcess(137);
	return 0;
}

/**
 * @brief Capture the original application process and start its lifetime watcher.
 * @return Zero on success, -1 for an invalid/dead owner or native startup failure.
 */
static int preload_worker_start_owner_watch(void)
{
	char value[32];
	DWORD count, pid = 0, i;
	HANDLE owner, thread;

	count = GetEnvironmentVariableA("GIT_PRELOAD_OWNER_PID",
					value, sizeof(value));
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
 * @brief Prepare owner monitoring and retain startup diagnostics.
 * @param directory Borrowed existing private worker directory.
 * @param location Receives an owned native pipe-name stem on success.
 * @return Zero on success, or a worker startup status on failure.
 */
int preload_worker_platform_prepare(const char *directory, char **location)
{
	preload_worker_directory = xstrdup(directory);
	{
		DWORD length = GetEnvironmentVariableW(L"GIT_TRACE_PRELOAD",
						       preload_worker_exit_log_path,
						       ARRAY_SIZE(preload_worker_exit_log_path));
		if (!length || length >= ARRAY_SIZE(preload_worker_exit_log_path) ||
		    !((preload_worker_exit_log_path[1] == L':' &&
		       (preload_worker_exit_log_path[2] == L'\\' ||
			preload_worker_exit_log_path[2] == L'/')) ||
		      (preload_worker_exit_log_path[0] == L'\\' &&
		       preload_worker_exit_log_path[1] == L'\\')))
			preload_worker_exit_log_path[0] = L'\0';
	}
	if (atexit(preload_worker_atexit_log)) {
		return 137;
	}
	if (preload_worker_start_owner_watch()) {
		return 136;
	}
	*location = xstrfmt("\\\\.\\pipe\\git-preload-%lu", (unsigned long)getpid());
	return 0;
}

/**
 * @brief Open an EOF-producing command input descriptor.
 * @return Owned binary NUL descriptor, or -1 with errno set.
 */
int preload_worker_open_input(void)
{
	return _open("NUL", _O_RDONLY | _O_BINARY);
}

/**
 * @brief Synchronize Win32 standard handles after CRT descriptor replacement.
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
 * @param directory Borrowed directory created by shared worker startup.
 */
void preload_worker_platform_cleanup(const char *directory)
{
	if (rmdir(directory) && errno != ENOENT)
		trace_printf_key(&preload_worker_trace, "worker directory cleanup failed: %s errno=%d\n",
				 directory, errno);
}
