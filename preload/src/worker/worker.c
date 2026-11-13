#include "git-compat-util.h"
#include "abspath.h"
#include "common-init.h"
#include "worker/context.h"
#include "worker/worker.h"
#include "shared/transport.h"
#include "trace.h"
#include "utf8.h"

struct trace_key preload_worker_trace = TRACE_KEY_INIT(PRELOAD);

/** Channel indices shared by both platform backends and completion handling. */
enum worker_channel {
	WORKER_STDIN, WORKER_STDOUT, WORKER_STDERR, WORKER_STATUS, WORKER_CONTEXT,
	WORKER_CHANNEL_COUNT
};

/** One single-use command; storage remains alive through normal CRT shutdown. */
struct preload_worker {
	struct preload_transport *transport; /**< Owned selected transport. */
	const char *directory; /**< Borrowed startup directory, valid through exit. */
	char *location; /**< Owned endpoint stem, borrowed by transport. */
	char *frame; /**< Owned request, retained while Git may reference argv. */
	size_t used; /**< Valid bytes in frame, excluding its final end field. */
	const char **args; /**< Owned argv array: entries borrowed from frame, plus NULL. */
	unsigned int redirected; /**< Bit mask of installed descriptors 0 through 2. */
};

static struct preload_worker *active_worker;
static void finish_worker_on_exit(int status);
static int run_worker(const char *directory, preload_command_fn command);

/**
 * Decode the protocol's unsigned little-endian integer without alignment assumptions.
 * @param bytes Borrowed buffer containing at least four readable bytes.
 * @return Decoded 32-bit value.
 */
static uint32_t request_u32(const char *bytes)
{
	const unsigned char *p = (const unsigned char *)bytes;

	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
		((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/**
 * Skip the NUL-terminated working-directory value.
 * @param frame Borrowed request bytes, bounded by used.
 * @param used Number of available bytes.
 * @param tag Borrowed, already terminated context field name.
 * @param pos In/out byte offset, initially the start of the field's value.
 * @return One for a complete value, zero for partial input, -1 for an unknown tag.
 */
static int skip_context_value(const char *frame, size_t used,
			      const char *tag, size_t *pos)
{
	const char *end;

	if (strcmp(tag, "cwd"))
		return -1;
	end = memchr(frame + *pos, '\0', used - *pos);
	if (!end)
		return 0;
	*pos = end - frame + 1;
	return 1;
}

/**
 * @brief Recognize the shared argv/context frame and explicit end field.
 *
 * @param frame Borrowed bytes; may contain an incomplete request.
 * @param used Number of readable bytes in frame.
 * @return One if complete, zero if incomplete, -1 for invalid framing.
 */
static int request_complete(const char *frame, size_t used)
{
	size_t pos = 4;
	unsigned int count, i;

	if (used < 4)
		return 0;
	count = request_u32(frame);
	/* The wire count excludes argv[0]; each string must end within the frame. */
	if (count > 65535)
		return -1;
	count++;
	for (i = 0; i < count; i++) {
		const char *end = memchr(frame + pos, '\0', used - pos);
		if (!end)
			return 0;
		pos = end - frame + 1;
	}
	while (pos < used) {
		const char *tag = frame + pos;
		const char *end = memchr(tag, '\0', used - pos);
		int result;

		if (!end)
			return 0;
		pos = end - frame + 1;
		if (!strcmp(tag, "end"))
			return pos == used ? 1 : -1;
		result = skip_context_value(frame, used, tag, &pos);
		if (result != 1)
			return result;
	}
	return 0;
}

/**
 * @brief Close only descriptors already redirected to command channels.
 *
 * @param mask Bit n identifies installed descriptor n, for n from zero to two.
 */
static void close_redirected_stdio(unsigned int mask)
{
	int fd;

	for (fd = STDOUT_FILENO; fd <= STDERR_FILENO; fd++) {
		if (!(mask & (1U << fd)))
			continue;
		if (fflush(fd == STDOUT_FILENO ? stdout : stderr) ||
		    preload_worker_flush_output(fd))
			trace_printf_key(&preload_worker_trace,
					 "output flush failed fd=%d errno=%d\n", fd, errno);
		if (close(fd))
			trace_printf_key(&preload_worker_trace,
					 "output close failed fd=%d errno=%d\n", fd, errno);
	}
	if (mask & 1U)
		close(STDIN_FILENO);
}

/**
 * @brief Write and flush the complete status record, retrying interrupted writes.
 *
 * @param transport Borrowed connected transport; all channels remain owned by it.
 * @param channel Zero-based status channel index; left open on success or failure.
 * @param status Command exit value to encode as an EXIT record.
 * @return Zero on delivery and flush success, -1 on failure, with errno set.
 */
static int write_status(struct preload_transport *transport,
			size_t channel, int status)
{
	char record[64];
	size_t length = xsnprintf(record, sizeof(record), "EXIT %d\n", status);
	size_t offset = 0;

	while (offset < length) {
		ssize_t count = preload_transport_write(transport, channel,
						       record + offset, length - offset);
		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0) {
			if (!count)
				errno = EIO;
			return -1;
		}
		offset += count;
	}
	return preload_transport_flush(transport, channel);
}

/**
 * @brief Deliver the command result and close its status channel.
 *
 * @param transport Borrowed connected transport; retains other channels.
 * @param channel Zero-based status channel index in transport's configuration.
 * @param status Command exit value to encode as an EXIT record.
 * @return Zero on delivery and flush success, -1 on failure, with errno set.
 */
static int send_status(struct preload_transport *transport,
		       size_t channel, int status)
{
	int result = write_status(transport, channel, status);

	if (result)
		trace_printf_key(&preload_worker_trace,
				 "status delivery failed rc=%d errno=%d\n", status, errno);
	preload_transport_close_channel(transport, channel);
	return result;
}

/**
 * @brief Run ordered completion when exiting an active preload command.
 *
 * @param status Git's command exit value, supplied by common_exit().
 */
void preload_worker_on_exit(int status)
{
	if (active_worker)
		finish_worker_on_exit(status);
}

/**
 * @brief Prepare the shared channel table and prestart backend connections.
 *
 * @param worker Owned command state with a valid private directory.
 * @return Zero on success, or a startup failure status.
 */
static int start_worker_transport(struct preload_worker *worker)
{
	static const struct preload_channel_spec channels[] = {
		{ "stdin", 0 }, { "stdout", 1 }, { "stderr", 1 },
		{ "exit-code", 1 }, { "params-cwd", 0 }
	};
	/*
	 * The request comes first: it tells the worker what to run. The standard
	 * streams follow in descriptor order, then the status channel.
	 */
	static const size_t order[] = {
		WORKER_CONTEXT, WORKER_STDIN, WORKER_STDOUT, WORKER_STDERR, WORKER_STATUS
	};
	struct preload_transport_config config = { 0 };
	int status = preload_worker_platform_prepare(worker->directory, &worker->location);

	if (status)
		return status;
	config.location = worker->location;
	config.channels = channels;
	config.order = order;
	config.nr = ARRAY_SIZE(channels);
	if (preload_transport_start(&worker->transport, &config))
		return 121;
	printf("READY %lu\n", (unsigned long)getpid());
	fflush(stdout);
	trace_printf_key(&preload_worker_trace, "pid=%lu ready\n", (unsigned long)getpid());
	if (preload_transport_wait(worker->transport) != PRELOAD_TRANSPORT_OK)
		return 122;
	/* Every marker has been read: the client may now close or write stdin. */
	return preload_worker_acknowledge_stdin(worker->transport, WORKER_STDIN) ? 122 : 0;
}

/**
 * @brief Read a bounded argv/context request through its explicit end field.
 *
 * @param worker Owned state; receives frame storage and validated byte length.
 * @return Zero on success, 123 for I/O failure or 124 for invalid framing.
 */
static int read_worker_request(struct preload_worker *worker)
{
	const size_t capacity = 131072;

	worker->frame = xmalloc(capacity);
	while (worker->used < capacity) {
		ssize_t n = preload_transport_read(worker->transport, WORKER_CONTEXT,
			worker->frame + worker->used, capacity - worker->used);
		int complete;

		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0)
			return 123;
		if (!n)
			return 124;
		worker->used += n;
		complete = request_complete(worker->frame, worker->used);
		if (complete < 0)
			return 124;
		if (complete) {
			worker->used -= sizeof("end");
			preload_transport_close_channel(worker->transport, WORKER_CONTEXT);
			return 0;
		}
	}
	return 124;
}

/**
 * @brief Decode already framed UTF-8 arguments and apply the command context.
 *
 * @param worker Owned validated request; args borrow its frame storage.
 * @return Argument count on success, or -1 for invalid arguments/context.
 */
static int apply_worker_request(struct preload_worker *worker)
{
	unsigned int i, count = request_u32(worker->frame) + 1;
	size_t pos = 4;

	CALLOC_ARRAY(worker->args, count + 1);
	for (i = 0; i < count; i++) {
		worker->args[i] = worker->frame + pos;
		if (!is_utf8(worker->args[i]))
			return -1;
		pos += strlen(worker->args[i]) + 1;
	}
	if (strcmp(worker->args[0], "git") ||
	    preload_context_apply(worker->frame + pos, worker->used - pos))
		return -1;
	return count;
}

/**
 * @brief Install command stdio, tracking each successful descriptor transfer.
 *
 * The caller's stdin channel becomes fd 0. A caller with no input closes its
 * end (after the Windows ACK), so the command reads end-of-file.
 *
 * @param worker Owned selected transport; redirected records partial progress.
 * @return Zero on success, -1 on descriptor or platform-handle failure.
 */
static int install_worker_stdio(struct preload_worker *worker)
{
	static const size_t channels[] = { WORKER_STDIN, WORKER_STDOUT, WORKER_STDERR };
	int target;

	for (target = 0; target < 3; target++) {
		int fd = preload_transport_detach_fd(worker->transport, channels[target]);
		int result;

		if (fd < 0)
			return -1;
		result = dup2(fd, target);
		if (fd != target)
			close(fd);
		if (result < 0)
			return -1;
		worker->redirected |= 1U << target;
	}
	return preload_worker_platform_stdio();
}

/**
 * @brief Close output before delivering status, including partial setup errors.
 *
 * @param worker Owned state; releases transport/location and filesystem endpoints.
 * @param status Requested command or startup exit value.
 * @return status, or 138 if the accepted status channel could not deliver it.
 */
static int finish_worker(struct preload_worker *worker, int status)
{
	size_t i;

	close_redirected_stdio(worker->redirected);
	if (worker->transport) {
		for (i = 0; i < WORKER_CHANNEL_COUNT; i++)
			if (i != WORKER_STATUS)
				preload_transport_close_channel(worker->transport, i);
		if (preload_transport_has_channel(worker->transport, WORKER_STATUS) &&
		    send_status(worker->transport, WORKER_STATUS, status))
			status = 138;
		preload_transport_release(worker->transport);
		worker->transport = NULL;
	}
	preload_worker_platform_cleanup(worker->directory);
	free(worker->location);
	worker->location = NULL;
	return status;
}

/**
 * @brief Complete the active command before returning to ordinary CRT exit.
 *
 * @param status Command exit value supplied by common_exit().
 */
static void finish_worker_on_exit(int status)
{
	struct preload_worker *worker = active_worker;

	active_worker = NULL;
	if (finish_worker(worker, status) == 138 && status != 138)
		_exit(138);
}

/**
 * @brief Prepare the transport, request and stdio for one worker command.
 * @param worker Owned zero-initialized state with its directory set; retains
 *   partial resources on failure for the caller to release with finish_worker().
 * @param argc Receives the decoded argument count on success, including argv[0].
 * @return Zero on success, or the worker startup/request failure status.
 */
static int prepare_worker(struct preload_worker *worker, int *argc)
{
	int status = start_worker_transport(worker);

	if (status)
		return status;
	status = read_worker_request(worker);
	if (status)
		return status;
	*argc = apply_worker_request(worker);
	if (*argc < 0)
		return 128;
	if (install_worker_stdio(worker))
		return 130;
	return 0;
}

/**
 * @brief Execute one command with shared framing, stdio and completion handling.
 *
 * @param directory Borrowed private worker directory, valid through process exit.
 * @param command Entry point that runs the requested command; never NULL.
 * @return Startup failure status; successful commands leave through normal exit.
 */
static int run_worker(const char *directory, preload_command_fn command)
{
	struct preload_worker worker = { 0 };
	int status, count;

	worker.directory = directory;
	status = prepare_worker(&worker, &count);
	if (status) {
		trace_printf_key(&preload_worker_trace, "worker request/startup failed status=%d errno=%d\n",
				 status, errno);
		status = finish_worker(&worker, status);
		free(worker.args);
		free(worker.frame);
		return status;
	}
	active_worker = &worker;
	init_git(worker.args);
	/* Git and CRT exit handlers may still reference argv and its request storage. */
	exit(command(count, worker.args));
}

/**
 * @brief Dispatch the exclusive startup mode without initializing a repository.
 *
 * @param argc Number of argv entries, including git and --preload-worker.
 * @param argv Borrowed startup arguments; exactly one session directory is required.
 * @param command Entry point that runs the requested command; never NULL.
 * @return Startup failure status; commands complete through normal process exit.
 */
int preload_worker_main(int argc, const char **argv, preload_command_fn command)
{
	char *directory;
	int result;

	if (argc != 3) {
		fprintf(stderr, "error: --preload-worker requires a session directory\n");
		return 129;
	}
	if (!is_directory(argv[2]))
		return error("--preload-worker session directory does not exist: %s", argv[2]);
	directory = xstrfmt("%s/worker-%lu", argv[2], (unsigned long)getpid());
	if (mkdir(directory, 0700)) {
		result = error_errno("--preload-worker cannot create %s", directory);
	} else {
		result = run_worker(directory, command);
		rmdir(directory);
	}
	free(directory);
	return result;
}
