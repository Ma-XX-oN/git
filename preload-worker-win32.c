#include "git-compat-util.h"
#include "common-init.h"
#include "preload-worker.h"
#include "preload-transport.h"
#include "preload-context.h"
#include "trace.h"

static struct preload_transport *preload_worker_status_transport;

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
/** @brief Append retained shutdown evidence without relying on trace descriptors. */
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

/*
 * The standard Git builtin dispatcher calls exit(ret) from git.c.
 * The temporary benchmark build intercepts that dispatcher exit only, so
 * the one-shot worker can flush its native pipes and deliver the status.
 */
/*
 * Preserve Git's existing common_exit() tracing on the caller side.
 * Return to the existing CRT exit() call after completion, without tracing twice.
 */
#undef exit
/** @brief Write, flush and close one owned status pipe after output EOF. */
static int preload_worker_send_status(struct preload_transport *transport, int status)
{
	char record[64];
	DWORD count = 0, error = 0;
	int length = snprintf(record, sizeof(record), "EXIT %d\n", status);

	trace_printf_key(&preload_worker_trace,
			 "pid=%lu status-write begin rc=%d\n", GetCurrentProcessId(), status);
	if (length <= 0 || (size_t)length >= sizeof(record))
		error = ERROR_INVALID_DATA;
	else if ((count = preload_transport_write(transport, 3, record, length)) == (DWORD)-1)
		error = GetLastError();
	else if (count != (DWORD)length)
		error = ERROR_WRITE_FAULT;
	else {
		trace_printf_key(&preload_worker_trace,
				 "pid=%lu status-flush begin bytes=%lu\n", GetCurrentProcessId(), count);
		if (preload_transport_flush(transport, 3))
			error = GetLastError();
		else
			trace_printf_key(&preload_worker_trace,
					 "pid=%lu status-flush done\n", GetCurrentProcessId());
	}
	preload_transport_close_channel(transport, 3);
	if (error) {
		trace_printf_key(&preload_worker_trace,
				 "pid=%lu status delivery error=%lu rc=%d\n",
				 GetCurrentProcessId(), error, status);
		preload_worker_exit_diagnostic("exit status delivery FAILED", status);
		return -1;
	}
	preload_worker_exit_diagnostic("exit status delivered", status);
	return 0;
}

/** @brief Deliver output EOF and status before returning to normal CRT exit. */
static void preload_worker_finish(int status)
{
	DWORD pid = GetCurrentProcessId();

	preload_worker_set_exit_handler(NULL);
	preload_worker_exit_diagnostic("preload exit callback", status);
	trace_printf_key(&preload_worker_trace,
			 "pid=%lu preload exit callback status=%d\n", pid, status);
	/*
	 * Windows waits until readers consume all pipe bytes. Closing each
	 * descriptor then provides EOF. The client drains both concurrently.
	 */
	trace_printf_key(&preload_worker_trace, "pid=%lu flush stdout begin\n", pid);
	fflush(stdout);
	FlushFileBuffers((HANDLE)_get_osfhandle(_fileno(stdout)));
	_close(_fileno(stdout));
	trace_printf_key(&preload_worker_trace, "pid=%lu flush stderr begin\n", pid);
	fflush(stderr);
	FlushFileBuffers((HANDLE)_get_osfhandle(_fileno(stderr)));
	_close(_fileno(stderr));
	trace_printf_key(&preload_worker_trace, "pid=%lu flush done\n", pid);
	_close(0);
	if (preload_worker_send_status(preload_worker_status_transport, status))
		ExitProcess(138);
}
#define exit(code) exit(common_exit(__FILE__, __LINE__, (code)))
/*
 * Experimental preinitialized Git worker.  The PID identifies four
 * two instances per channel; the Python manager never forwards command bytes.
 */
/*
 * Git Bash can open a throwaway pipe instance during redirection.
 * Two threads, created before READY, traverse all five channels themselves.
 * They connect and read one marker each; pipes remain synchronous for Git I/O.
 */
/*
 * The manager transfers each worker to git-ipc at allocation time. Keep an
 * independent reference to the owning Bash process so an abandoned worker
 * terminates even if it is blocked on a named pipe.
 *
 * Waiting on the process handle is stronger than polling its PID: it wakes
 * when the original process exits and cannot confuse a reused numeric PID.
 */
/** @brief Terminate an abandoned worker when the captured owner handle signals. */
static DWORD WINAPI preload_worker_watch_owner(LPVOID context)
{
	HANDLE owner = (HANDLE)context;
	WaitForSingleObject(owner, INFINITE);
	preload_worker_exit_diagnostic("ExitProcess owner-death", 137);
	ExitProcess(137);
	return 0;
}

/** @brief Capture the original shell process and start its lifetime watcher. */
static int preload_worker_start_owner_watch(void)
{
	char value[32];
	DWORD count, pid = 0, i;
	HANDLE owner, thread;

	count = GetEnvironmentVariableA("GIT_PRELOAD_OWNER_WINPID",
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
 * Return 1 for a complete Windows request, 0 for a partial request, and -1
 * for invalid framing. The final end tag follows argv and context fields;
 * it lets the client retain the control handle without withholding EOF.
 */
static int preload_worker_request_complete(const char *frame, size_t used)
{
	size_t pos = 4;
	unsigned int count, i;

	if (used < 4)
		return 0;
	count = (unsigned int)(unsigned char)frame[0] |
		((unsigned int)(unsigned char)frame[1] << 8) |
		((unsigned int)(unsigned char)frame[2] << 16) |
		((unsigned int)(unsigned char)frame[3] << 24);
	if (!count || count > 32)
		return -1;
	for (i = 0; i < count; i++) {
		const char *end = memchr(frame + pos, '\0', used - pos);
		if (!end)
			return 0;
		pos = end - frame + 1;
	}
	while (pos < used) {
		const char *tag = frame + pos;
		const char *end = memchr(tag, '\0', used - pos);
		if (!end)
			return 0;
		pos = end - frame + 1;
		if (!strcmp(tag, "end"))
			return pos == used ? 1 : -1;
		if (!strcmp(tag, "env")) {
			size_t size;
			if (used - pos < 4)
				return 0;
			size = (unsigned char)frame[pos] |
			       ((unsigned long)(unsigned char)frame[pos + 1] << 8) |
			       ((unsigned long)(unsigned char)frame[pos + 2] << 16) |
			       ((unsigned long)(unsigned char)frame[pos + 3] << 24);
			pos += 4;
			if (size > used - pos)
				return 0;
			pos += size;
		} else if (!strcmp(tag, "cwd") || !strcmp(tag, "cwd-win") ||
			   !strcmp(tag, "env-export")) {
			end = memchr(frame + pos, '\0', used - pos);
			if (!end)
				return 0;
			pos = end - frame + 1;
		} else {
			return -1;
		}
	}
	return 0;
}

/** @brief Accept one marked request through synchronous Windows pipe handles. */
int preload_worker_run(const char *directory UNUSED)
{
	static const struct preload_channel_spec channels[] = {
		{ "stdin", 0 }, { "stdout", 1 }, { "stderr", 1 }, { "exit-code", 1 }, { "param-env-cwd", 0 }
	};
	static const size_t order[] = { 1, 2, 3, 0, 4 };
	struct preload_transport *transport = NULL;
	struct preload_transport_config config = { 0 };
	char location[96];
	char *frame = NULL;
	const DWORD frame_capacity = 131072;
	DWORD used = 0;
	ssize_t n;
	int request_complete = 0;
	int i, count, pos, rc = 120, infd, outfd, errfd;
	const char *args[33] = { 0 };
	DWORD pid = GetCurrentProcessId();

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
		rc = 137;
		goto cleanup;
	}
	if (preload_worker_start_owner_watch()) {
		rc = 136;
		goto cleanup;
	}
	frame = malloc(frame_capacity);
	if (!frame) {
		rc = 135;
		goto cleanup;
	}

	snprintf(location, sizeof(location), "\\\\.\\pipe\\git-preload-%lu", (unsigned long)pid);
	config.location = location;
	config.channels = channels;
	config.order = order;
	config.nr = ARRAY_SIZE(channels);
	config.timeout_ms = 2000;
	if (preload_transport_start(&transport, &config)) {
		rc = 121;
		goto cleanup;
	}
	/* READY is an initialization handshake, not Git command output. */
	printf("READY %lu\n", (unsigned long)pid);
	fflush(stdout);
	trace_printf_key(&preload_worker_trace, "pid=%lu ready\n", pid);
	/* The connection threads retain the established channel-open order. */
	if (preload_transport_wait(transport) != PRELOAD_TRANSPORT_OK) {
		rc = 122;
		goto cleanup;
	}
	while (used < frame_capacity) {
		int complete;
		trace_printf_key(&preload_worker_trace,
				 "pid=%lu request-read begin used=%lu\n", pid, used);
		if ((n = preload_transport_read(transport, 4, frame + used, frame_capacity - used)) < 0) {
			DWORD error = GetLastError();
			trace_printf_key(&preload_worker_trace,
					 "pid=%lu request-read error=%lu used=%lu\n", pid, error, used);
			if (error != ERROR_BROKEN_PIPE) {
				rc = 123;
				goto cleanup;
			}
			break;
		}
		trace_printf_key(&preload_worker_trace,
				 "pid=%lu request-read bytes=%lu\n", pid, (unsigned long)n);
		if (!n)
			break;
		used += n;
		complete = preload_worker_request_complete(frame, used);
		if (complete < 0) {
			rc = 124;
			goto cleanup;
		}
		if (complete) {
			used -= sizeof("end");
			request_complete = 1;
			break;
		}
	}
	preload_transport_close_channel(transport, 4);
	trace_printf_key(&preload_worker_trace,
			 "pid=%lu request-complete bytes=%lu\n", pid, used);
	if (!request_complete || used < 4 || used == frame_capacity) {
		rc = 124;
		goto cleanup;
	}
	count = (unsigned char)frame[0] | ((unsigned char)frame[1] << 8) |
		((unsigned char)frame[2] << 16) | ((unsigned char)frame[3] << 24);
	if (count < 1 || count > 32) {
		rc = 125;
		goto cleanup;
	}
	pos = 4;
	for (i = 0; i < count; i++) {
		int start = pos;
		if (pos >= used) {
			rc = 126;
			goto cleanup;
		}
		while (pos < used && frame[pos])
			pos++;
		if (pos == used || (pos > start &&
				    !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
							 frame + start, pos - start, NULL, 0))) {
			rc = 127;
			goto cleanup;
		}
		args[i] = frame + start;
		pos++;
	}
	if (strcmp(args[0], "git") ||
	    preload_context_apply(frame + pos, used - pos)) {
		rc = 128;
		goto cleanup;
	}
	args[count] = NULL;
	/*
	 * The complete request frame has been read; its writer may remain open.
	 * Application stdin forwarding is unavailable: provide EOF from NUL.
	 * The client keeps the stdin transport open until output completion so
	 * its connection marker cannot be lost before select_transport reads it.
	 */
	preload_transport_close_channel(transport, 0);
	infd = _open("NUL", _O_RDONLY | _O_BINARY);
	if (infd < 0) {
		rc = 132;
		goto cleanup;
	}
	if (_dup2(infd, 0) < 0) {
		_close(infd);
		rc = 133;
		goto cleanup;
	}
	_close(infd);

	/* Transfer output handles to CRT descriptors used by Git's main(). */
	outfd = preload_transport_detach_fd(transport, 1);
	if (outfd < 0) {
		rc = 129;
		goto cleanup;
	}
	errfd = preload_transport_detach_fd(transport, 2);
	if (errfd < 0) {
		_close(outfd);
		rc = 130;
		goto cleanup;
	}
	if (_dup2(outfd, _fileno(stdout)) < 0 ||
	    _dup2(errfd, _fileno(stderr)) < 0) {
		_close(outfd);
		_close(errfd);
		rc = 131;
		goto cleanup;
	}
	_close(outfd);
	_close(errfd);
	if (!SetStdHandle(STD_INPUT_HANDLE, (HANDLE)_get_osfhandle(0)) ||
	    !SetStdHandle(STD_OUTPUT_HANDLE, (HANDLE)_get_osfhandle(1)) ||
	    !SetStdHandle(STD_ERROR_HANDLE, (HANDLE)_get_osfhandle(2))) {
		rc = 134;
		goto cleanup;
	}
	preload_worker_status_transport = transport;
	trace_printf_key(&preload_worker_trace,
			 "pid=%lu command begin argc=%d\n", pid, count);
	preload_worker_set_exit_handler(preload_worker_finish);
	init_git(args);
	rc = cmd_main(count, args);
	trace_printf_key(&preload_worker_trace,
			 "pid=%lu cmd_main returned rc=%d\n", pid, rc);
	rc = common_exit(__FILE__, __LINE__, rc);
	preload_worker_set_exit_handler(NULL);
	preload_worker_status_transport = NULL;
	trace_printf_key(&preload_worker_trace,
			 "pid=%lu command done rc=%d\n", pid, rc);
cleanup:
	trace_printf_key(&preload_worker_trace, "pid=%lu cleanup rc=%d\n", pid, rc);
	/* The client drains output before reading status, including on failure. */
	if (transport) {
		preload_transport_wait(transport);
		for (i = 0; i < 5; i++)
			if (i != 3)
				preload_transport_close_channel(transport, i);
		/* The completion handler has already closed status after a command. */
		if (preload_transport_has_channel(transport, 3) &&
		    preload_worker_send_status(transport, rc))
			rc = 138;
		preload_transport_release(transport);
	}
	trace_printf_key(&preload_worker_trace, "pid=%lu cleanup handles closed\n", pid);
	free(frame);
	return rc;
}
