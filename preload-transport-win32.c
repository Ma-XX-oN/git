#include "git-compat-util.h"
#include "preload-transport.h"
#include "preload-worker.h"
#include "strbuf.h"
#include "trace.h"

enum preload_transport_candidate_result {
	CANDIDATE_CANCELED,
	CANDIDATE_MARKED,
	CANDIDATE_ERROR
};

struct preload_transport_channel {
	LONG winner;
	enum preload_transport_candidate_result result[2];
	char marker[2];
};

struct preload_transport;

struct preload_transport_connector {
	struct preload_transport *connections;
	int index;
};

struct preload_transport {
	HANDLE (*pipes)
	[2];
	HANDLE *accepted, threads[2];
	struct preload_transport_config config;
	struct preload_transport_channel *channels;
	struct preload_transport_connector connectors[2];
	LONG arrived[2], completed, failure, deadline;
	char expected_marker;
};

/** @brief Read shared progress without modifying it. */
static LONG preload_transport_atomic_read(LONG *value)
{
	return InterlockedCompareExchange(value, 0, 0);
}

/** @brief Connect one candidate and consume exactly one marker byte. */
static void preload_transport_connect_candidate(struct preload_transport *connections,
						int channel, int index)
{
	struct preload_transport_channel *state = &connections->channels[channel];
	HANDLE pipe = connections->pipes[channel][index];

	state->result[index] = CANDIDATE_CANCELED;
	while (!preload_transport_atomic_read(&connections->failure) &&
	       !preload_transport_atomic_read(&state->winner)) {
		DWORD n = 0, error = 0;

		if (!ConnectNamedPipe(pipe, NULL))
			error = GetLastError();
		if (!error || error == ERROR_PIPE_CONNECTED) {
			if (preload_transport_atomic_read(&connections->failure) ||
			    preload_transport_atomic_read(&state->winner))
				return;
			if (ReadFile(pipe, &state->marker[index], 1, &n, NULL)) {
				if (n == 1) {
					state->result[index] = CANDIDATE_MARKED;
					InterlockedCompareExchange(&state->winner,
								   index + 1, 0);
					return;
				}
				error = ERROR_BROKEN_PIPE;
			} else {
				error = GetLastError();
			}
		}
		if (preload_transport_atomic_read(&connections->failure) ||
		    preload_transport_atomic_read(&state->winner))
			return;
		if (error != ERROR_BROKEN_PIPE && error != ERROR_NO_DATA &&
		    error != ERROR_PIPE_NOT_CONNECTED) {
			state->result[index] = CANDIDATE_ERROR;
			InterlockedCompareExchange(&connections->failure,
						   PRELOAD_TRANSPORT_SYSTEM_ERROR, 0);
			trace_printf_key(&preload_worker_trace,
					 "pid=%lu connection error=%lu channel=%s index=%d\n",
					 GetCurrentProcessId(), error,
					 connections->config.channels[channel].name, index);
			return;
		}
		/* A disconnected open probe is not a failed application connection. */
		DisconnectNamedPipe(pipe);
	}
}

/** @brief Inspect both completed candidates and publish this channel's result. */
static void preload_transport_finish_channel(struct preload_transport *connections,
					     int channel)
{
	struct preload_transport_channel *state = &connections->channels[channel];
	int index, seen = 0, chosen = -1, result = PRELOAD_TRANSPORT_OK;

	for (index = 0; index < 2; index++) {
		if (state->result[index] == CANDIDATE_CANCELED) {
			DWORD available = 0, n = 0;
			HANDLE pipe = connections->pipes[channel][index];

			/*
			 * Cancellation may beat a read whose marker is buffered.
			 * Both operations have returned: inspect that byte once,
			 * without readiness polling or reading application bytes.
			 */
			if (PeekNamedPipe(pipe, NULL, 0, NULL, &available, NULL) &&
			    available &&
			    ReadFile(pipe, &state->marker[index], 1, &n, NULL) &&
			    n == 1)
				state->result[index] = CANDIDATE_MARKED;
		}
		if (state->result[index] == CANDIDATE_MARKED) {
			seen++;
			chosen = index;
		}
	}
	if (seen == 2) {
		trace_printf_key(&preload_worker_trace,
				 "pid=%lu duplicate markers first=%u second=%u expected=%u channel=%s\n",
				 GetCurrentProcessId(), (unsigned char)state->marker[0],
				 (unsigned char)state->marker[1],
				 (unsigned char)connections->expected_marker,
				 connections->config.channels[channel].name);
		result = PRELOAD_TRANSPORT_DUPLICATE_MARKER;
	} else if (seen == 1) {
		char marker = state->marker[chosen];

		if (marker < '0' || marker > '9' ||
		    (connections->expected_marker &&
		     marker != connections->expected_marker)) {
			result = PRELOAD_TRANSPORT_INVALID_MARKER;
		} else {
			connections->expected_marker = marker;
			connections->accepted[channel] =
				connections->pipes[channel][chosen];
			InterlockedExchange(&connections->deadline,
					    (LONG)(GetTickCount() + connections->config.timeout_ms));
		}
	} else {
		result = PRELOAD_TRANSPORT_SYSTEM_ERROR;
	}
	if (result != PRELOAD_TRANSPORT_OK)
		InterlockedCompareExchange(&connections->failure, result, 0);
	trace_printf_key(&preload_worker_trace,
			 "pid=%lu select done channel=%s result=%d selected=%d marker=%u\n",
			 GetCurrentProcessId(), connections->config.channels[channel].name,
			 result, chosen, chosen < 0 ? 0 : (unsigned char)state->marker[chosen]);
}

/**
 * @brief Rendezvous after both candidate operations, without main-thread dispatch.
 *
 * Both threads publish their arrival. The marker winner cancels the peer and
 * spins until the peer arrives; if no operation is pending yet, retry until
 * cancellation is accepted or the peer arrives. The winner then inspects both
 * results and publishes completion. Keeping cancellation and marker inspection
 * on one thread prevents a late cancel from interrupting duplicate inspection.
 */
static int preload_transport_rendezvous(struct preload_transport *connections,
					int channel, int step, int index)
{
	int cancellation_requested = 0;

	InterlockedExchange(&connections->arrived[index], step);
	if (preload_transport_atomic_read(&connections->channels[channel].winner) == index + 1) {
		while (preload_transport_atomic_read(&connections->arrived[1 - index]) < step) {
			if (preload_transport_atomic_read(&connections->failure))
				return -1;
			if (!cancellation_requested) {
				if (CancelIoEx(connections->pipes[channel][1 - index],
					       NULL)) {
					cancellation_requested = 1;
				} else if (GetLastError() != ERROR_NOT_FOUND) {
					InterlockedCompareExchange(&connections->failure,
								   PRELOAD_TRANSPORT_SYSTEM_ERROR, 0);
					return -1;
				}
			}
			YieldProcessor();
		}
		preload_transport_finish_channel(connections, channel);
		InterlockedExchange(&connections->completed, step);
	} else {
		while (preload_transport_atomic_read(&connections->completed) < step) {
			if (preload_transport_atomic_read(&connections->failure))
				return -1;
			YieldProcessor();
		}
	}
	return preload_transport_atomic_read(&connections->failure) ? -1 : 0;
}

/** @brief Traverse every channel, then return normally to signal thread completion. */
static DWORD WINAPI preload_transport_connect_channels(LPVOID data)
{
	struct preload_transport_connector *connector = data;
	struct preload_transport *connections = connector->connections;
	const size_t *order = connections->config.order;
	int step;

	for (step = 0; step < connections->config.nr; step++) {
		preload_transport_connect_candidate(connections, order[step], connector->index);
		if (preload_transport_atomic_read(&connections->failure) ||
		    preload_transport_rendezvous(connections, order[step], step + 1,
						 connector->index))
			break;
	}
	return 0;
}

/** @brief Pass the complete handle table to two threads before publishing READY. */
static int preload_transport_start_connections(struct preload_transport *connections)
{
	int index;

	connections->deadline = (LONG)(GetTickCount() + connections->config.timeout_ms);
	for (index = 0; index < connections->config.nr; index++)
		connections->accepted[index] = INVALID_HANDLE_VALUE;
	for (index = 0; index < 2; index++) {
		connections->connectors[index].connections = connections;
		connections->connectors[index].index = index;
		connections->threads[index] = CreateThread(NULL, 0,
							   preload_transport_connect_channels, &connections->connectors[index], 0, NULL);
		if (!connections->threads[index]) {
			InterlockedCompareExchange(&connections->failure,
						   PRELOAD_TRANSPORT_SYSTEM_ERROR, 0);
			return -1;
		}
	}
	return 0;
}

/**
 * @brief Join both threads, close probes, and transfer accepted handles to the caller.
 *
 * Main waits for final completion. A deadline recheck is needed only if a wait
 * expires after the workers have advanced to another channel. On failure, retry
 * handle-targeted cancellation until both threads exit; storage and handles stay
 * alive throughout cancellation, including a call submitted just after a retry.
 */
static int preload_transport_finish_connections(struct preload_transport *connections)
{
	HANDLE threads[2];
	DWORD count = 0;
	int channel, index;

	for (index = 0; index < 2; index++)
		if (connections->threads[index])
			threads[count++] = connections->threads[index];
	while (count) {
		DWORD wait, timeout = 1;

		if (preload_transport_atomic_read(&connections->failure)) {
			for (channel = 0; channel < connections->config.nr; channel++)
				for (index = 0; index < 2; index++)
					CancelIoEx(connections->pipes[channel][index], NULL);
		} else {
			LONG remaining = (LONG)((DWORD)preload_transport_atomic_read(
							&connections->deadline) -
						GetTickCount());

			if (remaining <= 0) {
				InterlockedCompareExchange(&connections->failure,
							   PRELOAD_TRANSPORT_TIMEOUT, 0);
				continue;
			}
			timeout = remaining;
		}
		wait = WaitForMultipleObjects(count, threads, TRUE, timeout);
		if (wait == WAIT_OBJECT_0)
			break;
		if (wait != WAIT_TIMEOUT)
			BUG("cannot join Git IPC connection threads: %lu", GetLastError());
	}
	for (index = 0; index < 2; index++) {
		if (connections->threads[index])
			CloseHandle(connections->threads[index]);
		connections->threads[index] = NULL;
	}
	/* Keep handles stable until both threads have stopped using them. */
	for (channel = 0; channel < connections->config.nr; channel++) {
		for (index = 0; index < 2; index++)
			if (connections->pipes[channel][index] != INVALID_HANDLE_VALUE &&
			    connections->pipes[channel][index] != connections->accepted[channel])
				CloseHandle(connections->pipes[channel][index]);
		connections->pipes[channel][0] = INVALID_HANDLE_VALUE;
		connections->pipes[channel][1] = connections->accepted[channel];
	}
	return preload_transport_atomic_read(&connections->failure);
}

/** @brief Create owner-restricted synchronous pipes before publishing readiness. */
int preload_transport_start(struct preload_transport **out,
			    const struct preload_transport_config *config)
{
	struct preload_transport *transport;
	HANDLE token = NULL;
	TOKEN_USER *token_user = NULL;
	DWORD token_size = 0, acl_size, error = 0;
	ACL *acl = NULL;
	SECURITY_DESCRIPTOR security_descriptor;
	SECURITY_ATTRIBUTES security_attributes;
	size_t i, j;
	int result = -1;

	*out = NULL;
	if (!config || !config->location || !config->nr || config->nr > 32 ||
	    !config->channels || !config->order || !config->timeout_ms) {
		errno = EINVAL;
		return -1;
	}
	for (i = 0; i < config->nr; i++) {
		if (!config->channels[i].name || config->order[i] >= config->nr) {
			errno = EINVAL;
			return -1;
		}
		for (j = 0; j < i; j++)
			if (config->order[j] == config->order[i]) {
				errno = EINVAL;
				return -1;
			}
	}
	transport = xcalloc(1, sizeof(*transport));
	*out = transport;
	transport->config = *config;
	transport->expected_marker = config->expected_marker;
	transport->pipes = xcalloc(config->nr, sizeof(*transport->pipes));
	transport->accepted = xcalloc(config->nr, sizeof(*transport->accepted));
	transport->channels = xcalloc(config->nr, sizeof(*transport->channels));
	for (i = 0; i < config->nr; i++) {
		transport->accepted[i] = INVALID_HANDLE_VALUE;
		for (j = 0; j < 2; j++)
			transport->pipes[i][j] = INVALID_HANDLE_VALUE;
	}
	/*
	 * A pipe's default security descriptor gives Everyone and Anonymous
	 * read access. Restrict all five PID-addressed channels to the process
	 * owner before publishing their names in the READY handshake.
	 */
	if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token))
		goto done;
	GetTokenInformation(token, TokenUser, NULL, 0, &token_size);
	if (!token_size || GetLastError() != ERROR_INSUFFICIENT_BUFFER)
		goto done;
	token_user = malloc(token_size);
	if (!token_user ||
	    !GetTokenInformation(token, TokenUser, token_user, token_size,
				 &token_size))
		goto done;
	acl_size = sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) -
		   sizeof(DWORD) + GetLengthSid(token_user->User.Sid);
	acl = malloc(acl_size);
	if (!acl ||
	    !InitializeAcl(acl, acl_size, ACL_REVISION) ||
	    !AddAccessAllowedAce(acl, ACL_REVISION, GENERIC_ALL,
				 token_user->User.Sid) ||
	    !InitializeSecurityDescriptor(&security_descriptor,
					  SECURITY_DESCRIPTOR_REVISION) ||
	    !SetSecurityDescriptorDacl(&security_descriptor, TRUE, acl, FALSE))
		goto done;
	security_attributes.nLength = sizeof(security_attributes);
	security_attributes.lpSecurityDescriptor = &security_descriptor;
	security_attributes.bInheritHandle = FALSE;

	for (i = 0; i < config->nr; i++) {
		struct strbuf name = STRBUF_INIT;
		strbuf_addf(&name, "%s-%s", config->location, config->channels[i].name);
		for (j = 0; j < 2; j++) {
			transport->pipes[i][j] = CreateNamedPipeA(name.buf,
								  PIPE_ACCESS_DUPLEX | (j == 0 ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0),
								  PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
								  2, 65536, 65536, 5000, &security_attributes);
			if (transport->pipes[i][j] == INVALID_HANDLE_VALUE) {
				error = GetLastError();
				strbuf_release(&name);
				goto done;
			}
		}
		strbuf_release(&name);
	}
	result = preload_transport_start_connections(transport);
done:
	if (result && !error)
		error = GetLastError();
	free(acl);
	free(token_user);
	if (token)
		CloseHandle(token);
	if (result && error) {
		errno = err_win_to_posix(error);
		SetLastError(error);
	}
	return result;
}

/** @brief Join the two blocking selectors before application I/O begins. */
int preload_transport_wait(struct preload_transport *transport)
{
	return preload_transport_finish_connections(transport);
}

/** @brief Read from a selected synchronous application handle. */
ssize_t preload_transport_read(struct preload_transport *transport, size_t channel,
			       void *buffer, size_t size)
{
	DWORD count;
	if (ReadFile(transport->pipes[channel][1], buffer,
		     size > MAXDWORD ? MAXDWORD : (DWORD)size, &count, NULL))
		return count;
	if (GetLastError() == ERROR_BROKEN_PIPE)
		return 0;
	errno = err_win_to_posix(GetLastError());
	return -1;
}

/** @brief Write to a selected synchronous application handle. */
ssize_t preload_transport_write(struct preload_transport *transport, size_t channel,
				const void *buffer, size_t size)
{
	DWORD count;
	if (WriteFile(transport->pipes[channel][1], buffer,
		      size > MAXDWORD ? MAXDWORD : (DWORD)size, &count, NULL))
		return count;
	errno = err_win_to_posix(GetLastError());
	return -1;
}

/** @brief Wait for the peer to consume outstanding pipe bytes. */
int preload_transport_flush(struct preload_transport *transport, size_t channel)
{
	if (FlushFileBuffers(transport->pipes[channel][1]))
		return 0;
	errno = err_win_to_posix(GetLastError());
	return -1;
}

/** @brief Transfer a selected synchronous handle into an owned CRT descriptor. */
int preload_transport_detach_fd(struct preload_transport *transport, size_t channel)
{
	int fd = _open_osfhandle((intptr_t)transport->pipes[channel][1], _O_BINARY);
	if (fd >= 0) {
		transport->pipes[channel][1] = INVALID_HANDLE_VALUE;
		transport->accepted[channel] = INVALID_HANDLE_VALUE;
	}
	return fd;
}

/** @brief Close a connected channel still owned by this transport. */
int preload_transport_has_channel(struct preload_transport *transport, size_t channel)
{
	return transport->pipes[channel][1] != INVALID_HANDLE_VALUE;
}

/** @brief Close a connected channel still owned by this transport. */
void preload_transport_close_channel(struct preload_transport *transport, size_t channel)
{
	int index;
	for (index = 0; index < 2; index++) {
		if (transport->pipes[channel][index] != INVALID_HANDLE_VALUE) {
			DisconnectNamedPipe(transport->pipes[channel][index]);
			CloseHandle(transport->pipes[channel][index]);
			transport->pipes[channel][index] = INVALID_HANDLE_VALUE;
		}
	}
	transport->accepted[channel] = INVALID_HANDLE_VALUE;
}

/** @brief Cancel/join connection operations before freeing their shared storage. */
void preload_transport_release(struct preload_transport *transport)
{
	size_t channel;
	if (!transport)
		return;
	if (transport->threads[0] || transport->threads[1]) {
		InterlockedCompareExchange(&transport->failure, PRELOAD_TRANSPORT_SYSTEM_ERROR, 0);
		preload_transport_finish_connections(transport);
	}
	for (channel = 0; channel < transport->config.nr; channel++)
		preload_transport_close_channel(transport, channel);
	free(transport->pipes);
	free(transport->accepted);
	free(transport->channels);
	free(transport);
}
