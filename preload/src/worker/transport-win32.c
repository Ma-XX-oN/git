#include "git-compat-util.h"
#include "worker/transport.h"
#include "worker/worker.h"
#include "strbuf.h"
#include "trace.h"

enum preload_transport_candidate_result {
	CANDIDATE_CANCELED,
	CANDIDATE_MARKED,
	CANDIDATE_ERROR
};

/** Selection results for one logical channel, published at the rendezvous. */
struct preload_transport_channel {
	LONG winner; /**< Atomic: zero until selected, otherwise instance index + 1. */
	enum preload_transport_candidate_result result[2]; /**< Each connector owns its slot. */
	char marker[2]; /**< Consumed bytes; read by the winner after both arrivals. */
};

struct preload_transport;

/** Arguments borrowed by one connector until its thread has been joined. */
struct preload_transport_connector {
	struct preload_transport *connections; /**< Shared transport, owned by main. */
	int index; /**< Pipe instance and publication slot, zero or one. */
};

/** Owns endpoints and connector storage from preparation through thread join. */
struct preload_transport {
	HANDLE (*pipes)[2]; /**< Owned candidate handles, indexed by channel/instance. */
	HANDLE *accepted; /**< Aliases selected candidates; invalid after detach/close. */
	HANDLE threads[2]; /**< Owned thread handles, closed after joining. */
	struct preload_transport_config config; /**< Copy; referenced strings remain borrowed. */
	struct preload_transport_channel *channels; /**< Owned per-channel selection state. */
	struct preload_transport_connector connectors[2]; /**< Stable borrowed thread arguments. */
	LONG arrived[2]; /**< Atomic per-thread completed operation generation. */
	LONG completed; /**< Atomic generation validated by the winning connector. */
	LONG failure; /**< Atomic first preload_transport_result failure, or zero. */
	LONG deadline; /**< Atomic wrapping GetTickCount deadline in milliseconds. */
	char expected_marker; /**< Configured digit or first valid consumed marker. */
};

/**
 * @brief Read shared progress without modifying it.
 *
 * @param value Borrowed aligned atomic LONG shared by the connector threads.
 * @return Value observed with an interlocked memory barrier.
 */
static LONG atomic_read(LONG *value)
{
	return InterlockedCompareExchange(value, 0, 0);
}

/**
 * @brief Connect one candidate and consume exactly one marker byte.
 *
 * @param connections Borrowed live transport; caller is one of its connectors.
 * @param channel Configured logical channel index.
 * @param index Calling connector's instance index, zero or one.
 */
static void connect_candidate(struct preload_transport *connections,
			      int channel, int index)
{
	struct preload_transport_channel *state = &connections->channels[channel];
	HANDLE pipe = connections->pipes[channel][index];

	state->result[index] = CANDIDATE_CANCELED;
	while (!atomic_read(&connections->failure) &&
	       !atomic_read(&state->winner)) {
		DWORD n = 0, error = 0;

		if (!ConnectNamedPipe(pipe, NULL))
			error = GetLastError();
		if (!error || error == ERROR_PIPE_CONNECTED) {
			if (atomic_read(&connections->failure) ||
			    atomic_read(&state->winner))
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
		if (atomic_read(&connections->failure) ||
		    atomic_read(&state->winner))
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

/**
 * @brief Collect marker results after both connectors have arrived.
 *
 * @param connections Borrowed transport with both candidate operations returned.
 * @param channel Logical channel index in the configuration.
 * @param chosen Receives the last marked candidate index, or -1 if none.
 * @return Number of marked candidates, zero through two.
 */
static int collect_channel_markers(struct preload_transport *connections,
				   int channel, int *chosen)
{
	struct preload_transport_channel *state = &connections->channels[channel];
	int index, seen = 0;

	*chosen = -1;
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
			*chosen = index;
		}
	}
	return seen;
}
/**
 * @brief Inspect completed candidates and publish the channel's result.
 *
 * @param connections Borrowed shared state, accessed by the winning connector.
 * @param channel Configured logical channel index; both operations have returned.
 */
static void finish_channel(struct preload_transport *connections,
			   int channel)
{
	struct preload_transport_channel *state = &connections->channels[channel];
	int chosen, result = PRELOAD_TRANSPORT_OK;
	int seen = collect_channel_markers(connections, channel, &chosen);

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
			trace_printf_key(&preload_worker_trace,
					 "pid=%lu invalid marker=%u expected=%u channel=%s\n",
					 GetCurrentProcessId(), (unsigned char)marker,
					 (unsigned char)connections->expected_marker,
					 connections->config.channels[channel].name);
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
	if (result != PRELOAD_TRANSPORT_OK) {
		InterlockedCompareExchange(&connections->failure, result, 0);
		trace_printf_key(&preload_worker_trace,
				 "pid=%lu channel selection failed channel=%s result=%d\n",
				 GetCurrentProcessId(), connections->config.channels[channel].name,
				 result);
	}
}

/**
 * @brief Rendezvous after both candidate operations, without main-thread dispatch.
 *
 * Both threads publish their arrival. The marker winner cancels the peer and
 * spins until the peer arrives; if no operation is pending yet, retry until
 * cancellation is accepted or the peer arrives. The winner then inspects both
 * results and publishes completion. Keeping cancellation and marker inspection
 * on one thread prevents a late cancel from interrupting duplicate inspection.
 *
 * @param connections Borrowed live transport, shared by both connectors.
 * @param channel Configured logical channel index for this rendezvous.
 * @param step One-based channel generation in the configured connection order.
 * @param index Calling connector's instance index, zero or one.
 * @return Zero after peer completion, -1 if connection or cancellation failed.
 */
static int rendezvous(struct preload_transport *connections,
		      int channel, int step, int index)
{
	int cancellation_requested = 0;

	InterlockedExchange(&connections->arrived[index], step);
	if (atomic_read(&connections->channels[channel].winner) == index + 1) {
		while (atomic_read(&connections->arrived[1 - index]) < step) {
			if (atomic_read(&connections->failure))
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
		finish_channel(connections, channel);
		InterlockedExchange(&connections->completed, step);
	} else {
		while (atomic_read(&connections->completed) < step) {
			if (atomic_read(&connections->failure))
				return -1;
			YieldProcessor();
		}
	}
	return atomic_read(&connections->failure) ? -1 : 0;
}

/**
 * @brief Traverse channels, then return normally to signal thread completion.
 *
 * @param data Borrowed preload_transport_connector, stable until this thread is joined.
 * @return Zero; shared failure state records connection errors.
 */
static DWORD WINAPI connect_channels(LPVOID data)
{
	struct preload_transport_connector *connector = data;
	struct preload_transport *connections = connector->connections;
	const size_t *order = connections->config.order;
	int step;

	for (step = 0; step < connections->config.nr; step++) {
		connect_candidate(connections, order[step], connector->index);
		if (atomic_read(&connections->failure) ||
		    rendezvous(connections, order[step], step + 1,
						 connector->index))
			break;
	}
	return 0;
}

/**
 * @brief Pass the complete handle table to two threads before publishing READY.
 *
 * @param connections Borrowed initialized transport; retains owned thread handles.
 * @return Zero on success, -1 on thread creation failure; release still joins partial startup.
 */
static int start_connections(struct preload_transport *connections)
{
	int index;

	connections->deadline = (LONG)(GetTickCount() + connections->config.timeout_ms);
	for (index = 0; index < connections->config.nr; index++)
		connections->accepted[index] = INVALID_HANDLE_VALUE;
	for (index = 0; index < 2; index++) {
		connections->connectors[index].connections = connections;
		connections->connectors[index].index = index;
		connections->threads[index] = CreateThread(NULL, 0,
			connect_channels,
			&connections->connectors[index], 0, NULL);
		if (!connections->threads[index]) {
			InterlockedCompareExchange(&connections->failure,
						   PRELOAD_TRANSPORT_SYSTEM_ERROR, 0);
			return -1;
		}
	}
	return 0;
}

/**
 * @brief Wait for connector completion, retrying handle cancellation on failure.
 *
 * Main waits for final completion. A deadline recheck is needed only if a wait
 * expires after the workers have advanced to another channel. On failure, retry
 * handle-targeted cancellation until both threads exit; storage and handles stay
 * alive throughout cancellation, including a call submitted just after a retry.
 *
 * @param connections Borrowed live state; all handles remain owned until threads stop.
 */
static void join_connector_threads(struct preload_transport *connections)
{
	HANDLE threads[2];
	DWORD count = 0;
	int channel, index;

	for (index = 0; index < 2; index++)
		if (connections->threads[index])
			threads[count++] = connections->threads[index];
	while (count) {
		DWORD wait, timeout = 1;

		if (atomic_read(&connections->failure)) {
			for (channel = 0; channel < connections->config.nr; channel++)
				for (index = 0; index < 2; index++)
					CancelIoEx(connections->pipes[channel][index], NULL);
		} else {
			LONG remaining = (LONG)((DWORD)atomic_read(
							&connections->deadline) -
						GetTickCount());

			if (remaining <= 0) {
				trace_printf_key(&preload_worker_trace,
						 "pid=%lu connection timeout\n", GetCurrentProcessId());
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
}
/**
 * @brief Join connectors, discard probes and retain selected synchronous handles.
 *
 * @param connections Borrowed prepared state; relinquishes owned thread handles.
 * @return First connection failure, or PRELOAD_TRANSPORT_OK.
 */
static int finish_connections(struct preload_transport *connections)
{
	int channel, index;

	join_connector_threads(connections);
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
	return atomic_read(&connections->failure);
}

/**
 * @brief Own the security descriptor's backing allocations during pipe creation.
 * All pointers are released after CreateNamedPipe has copied the descriptor.
 */
struct preload_pipe_security {
	HANDLE token; /**< Owned access token for the current process. */
	TOKEN_USER *user; /**< Owned token information containing the owner's SID. */
	ACL *acl; /**< Owned ACL granting access to that SID. */
	SECURITY_DESCRIPTOR descriptor; /**< References acl until endpoint creation ends. */
	SECURITY_ATTRIBUTES attributes; /**< Borrows descriptor; disables inheritance. */
};

/**
 * @brief Check endpoint names and the connection-order permutation.
 *
 * @param config Borrowed configuration; must specify 1 through 32 channels.
 * @return One if valid, zero otherwise; leaves errno unchanged.
 */
static int is_valid_config(const struct preload_transport_config *config)
{
	size_t i, j;

	if (!config || !config->location || !config->nr || config->nr > 32 ||
	    !config->channels || !config->order || !config->timeout_ms ||
	    config->timeout_ms > INT_MAX ||
	    (config->expected_marker &&
	     (config->expected_marker < '0' || config->expected_marker > '9')))
		return 0;
	for (i = 0; i < config->nr; i++) {
		if (!config->channels[i].name || config->order[i] >= config->nr)
			return 0;
		for (j = 0; j < i; j++)
			if (config->order[j] == config->order[i])
				return 0;
	}
	return 1;
}

/**
 * @brief Allocate a transport whose endpoint slots are initially unowned.
 *
 * @param config Borrowed validated configuration; strings outlive the transport.
 * @return Newly owned transport, released with preload_transport_release().
 */
static struct preload_transport *allocate_transport(
	const struct preload_transport_config *config)
{
	struct preload_transport *transport = xcalloc(1, sizeof(*transport));
	size_t i, j;

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
	return transport;
}

/**
 * @brief Grant pipe access exclusively to the current process owner.
 *
 * @param security Zero-initialized owned storage, including on partial failure.
 * @return Zero on success, -1 with a Win32 last error on failure.
 *
 * A default pipe descriptor grants Everyone and Anonymous read access.
 * Construct the restricted descriptor before publishing endpoint readiness.
 */
static int prepare_pipe_security(struct preload_pipe_security *security)
{
	DWORD token_size = 0, acl_size;

	if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &security->token))
		return -1;
	GetTokenInformation(security->token, TokenUser, NULL, 0, &token_size);
	if (!token_size || GetLastError() != ERROR_INSUFFICIENT_BUFFER)
		return -1;
	security->user = xmalloc(token_size);
	if (!GetTokenInformation(security->token, TokenUser, security->user,
				 token_size, &token_size))
		return -1;
	acl_size = sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) -
		sizeof(DWORD) + GetLengthSid(security->user->User.Sid);
	security->acl = xmalloc(acl_size);
	if (!InitializeAcl(security->acl, acl_size, ACL_REVISION) ||
	    !AddAccessAllowedAce(security->acl, ACL_REVISION, GENERIC_ALL,
				 security->user->User.Sid) ||
	    !InitializeSecurityDescriptor(&security->descriptor,
					  SECURITY_DESCRIPTOR_REVISION) ||
	    !SetSecurityDescriptorDacl(&security->descriptor, TRUE,
				       security->acl, FALSE))
		return -1;
	security->attributes.nLength = sizeof(security->attributes);
	security->attributes.lpSecurityDescriptor = &security->descriptor;
	security->attributes.bInheritHandle = FALSE;
	return 0;
}

/**
 * @brief Create two synchronous duplex instances for each logical channel.
 *
 * @param transport Owned endpoint table, retaining partial results on error.
 * @param attributes Borrowed owner-restricted descriptor, valid during this call.
 * @return Zero on success, -1 with a Win32 last error on failure.
 */
static int create_pipe_instances(struct preload_transport *transport,
				SECURITY_ATTRIBUTES *attributes)
{
	size_t i, j;

	for (i = 0; i < transport->config.nr; i++) {
		struct strbuf name = STRBUF_INIT;

		/* The location is a pipe-name "directory"; channels are its entries. */
		strbuf_addf(&name, "%s\\%s", transport->config.location,
			    transport->config.channels[i].name);
		for (j = 0; j < 2; j++) {
			DWORD mode = PIPE_ACCESS_DUPLEX |
				(j == 0 ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0);

			transport->pipes[i][j] = CreateNamedPipeA(name.buf, mode,
				PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
				2, 65536, 65536, 5000, attributes);
			if (transport->pipes[i][j] == INVALID_HANDLE_VALUE) {
				DWORD error = GetLastError();

				strbuf_release(&name);
				SetLastError(error);
				return -1;
			}
		}
		strbuf_release(&name);
	}
	return 0;
}

/**
 * @brief Prepare owned endpoints and prestart the two blocking connectors.
 *
 * @param out Receives owned state, including partial state on failure; release it.
 * @param config Borrowed configuration; referenced strings outlive returned state.
 * @return Zero on success, -1 with errno set on failure.
 */
int preload_transport_start(struct preload_transport **out,
			    const struct preload_transport_config *config)
{
	struct preload_pipe_security security = { 0 };
	DWORD error;
	int result = -1;

	*out = NULL;
	if (!is_valid_config(config)) {
		errno = EINVAL;
		return -1;
	}
	*out = allocate_transport(config);
	if (!prepare_pipe_security(&security) &&
	    !create_pipe_instances(*out, &security.attributes))
		result = start_connections(*out);
	error = result ? GetLastError() : 0;
	free(security.acl);
	free(security.user);
	if (security.token)
		CloseHandle(security.token);
	if (result) {
		errno = err_win_to_posix(error);
		SetLastError(error);
	}
	return result;
}

/**
 * @brief Complete connections in the configured order before application I/O.
 *
 * @param transport Borrowed prepared state; invoke once before transferring channels.
 * @return A preload_transport_result; failed connections remain owned until release.
 */
int preload_transport_wait(struct preload_transport *transport)
{
	return finish_connections(transport);
}

/**
 * @brief Read application bytes from a connected input channel.
 *
 * @param transport Borrowed connected state.
 * @param channel Configured zero-based channel index, still owned by transport.
 * @param buffer Caller-owned destination with at least size writable bytes.
 * @param size Maximum byte count to read.
 * @return Bytes read, zero at EOF, or -1 with errno set; short reads are valid.
 */
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

/**
 * @brief Write application bytes to a connected output channel.
 *
 * @param transport Borrowed connected state.
 * @param channel Configured zero-based channel index, still owned by transport.
 * @param buffer Borrowed source with at least size readable bytes.
 * @param size Maximum byte count to write.
 * @return Bytes written, or -1 with errno set; callers handle short writes.
 */
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

/**
 * @brief Finish buffered delivery where the platform requires it.
 *
 * @param transport Borrowed connected state.
 * @param channel Configured output channel index, still owned by transport.
 * @return Zero on success, -1 with errno set on failure. Windows waits for consumption.
 */
int preload_transport_flush(struct preload_transport *transport, size_t channel)
{
	if (FlushFileBuffers(transport->pipes[channel][1]))
		return 0;
	errno = err_win_to_posix(GetLastError());
	return -1;
}

/**
 * @brief Transfer a connected channel to a caller-owned CRT/POSIX descriptor.
 *
 * @param transport Borrowed connected state; relinquishes the selected channel.
 * @param channel Configured channel index, still owned by transport.
 * @return Descriptor on success, -1 on failure, retaining ownership on failure.
 */
int preload_transport_detach_fd(struct preload_transport *transport, size_t channel)
{
	int fd = _open_osfhandle((intptr_t)transport->pipes[channel][1], _O_BINARY);
	if (fd >= 0) {
		transport->pipes[channel][1] = INVALID_HANDLE_VALUE;
		transport->accepted[channel] = INVALID_HANDLE_VALUE;
	}
	return fd;
}

/**
 * @param transport Borrowed prepared or connected state.
 * @param channel Configured zero-based channel index.
 * @return Nonzero if selected and still owned, zero before connection or after transfer/close.
 */
int preload_transport_has_channel(struct preload_transport *transport, size_t channel)
{
	return transport->accepted[channel] != INVALID_HANDLE_VALUE;
}

/**
 * @brief Close one channel after connection work has finished.
 *
 * @param transport Borrowed connected state; retains all other channels.
 * @param channel Configured zero-based index; closing an already closed channel is safe.
 */
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

/**
 * @brief Cancel/join pending connections and release all owned resources.
 *
 * @param transport Owned prepared or connected state, including partial startup; NULL is safe.
 */
void preload_transport_release(struct preload_transport *transport)
{
	size_t channel;
	if (!transport)
		return;
	if (transport->threads[0] || transport->threads[1]) {
		InterlockedCompareExchange(&transport->failure, PRELOAD_TRANSPORT_SYSTEM_ERROR, 0);
		finish_connections(transport);
	}
	for (channel = 0; channel < transport->config.nr; channel++)
		preload_transport_close_channel(transport, channel);
	free(transport->pipes);
	free(transport->accepted);
	free(transport->channels);
	free(transport);
}
