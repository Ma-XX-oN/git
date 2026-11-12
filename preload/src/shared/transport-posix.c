#include "git-compat-util.h"
#include "shared/transport.h"
#include "shared/connection.h"
#include "strbuf.h"

/** Owns connected FIFO descriptors and borrows immutable endpoint configuration. */
struct preload_transport {
	struct preload_transport_config config; /**< Copied configuration; strings remain borrowed. */
	int *fds; /**< Owned descriptor table, with -1 for unconnected or transferred entries. */
};

/**
 * @brief Prepare borrowed FIFO names created by the worker's endpoint preparation.
 *
 * @param out Receives owned state, or NULL on configuration failure; caller releases it.
 * @param config Borrowed configuration; referenced strings outlive the transport.
 * @return Zero on success, -1 with errno set for invalid configuration.
 */
int preload_transport_start(struct preload_transport **out,
			    const struct preload_transport_config *config)
{
	struct preload_transport *transport;
	size_t i, j;

	*out = NULL;
	if (!config || !config->location || !config->nr || config->nr > 32 ||
	    !config->channels || !config->order) {
		errno = EINVAL;
		return -1;
	}
	for (i = 0; i < config->nr; i++) {
		if (!config->channels[i].name || config->order[i] >= config->nr) {
			errno = EINVAL;
			return -1;
		}
		for (j = 0; j < i; j++)
			if (config->order[i] == config->order[j]) {
				errno = EINVAL;
				return -1;
			}
	}
	transport = xcalloc(1, sizeof(*transport));
	transport->config = *config;
	ALLOC_ARRAY(transport->fds, config->nr);
	for (i = 0; i < config->nr; i++)
		transport->fds[i] = -1;
	*out = transport;
	return 0;
}

/**
 * @brief Complete connections in the configured order before application I/O.
 *
 * @param transport Borrowed prepared state; invoke once before transferring channels.
 * @return A preload_transport_result; failed connections remain owned until release.
 */
int preload_transport_wait(struct preload_transport *transport)
{
	size_t step;
	for (step = 0; step < transport->config.nr; step++) {
		size_t channel = transport->config.order[step];
		struct strbuf path = STRBUF_INIT;
		int mode = transport->config.channels[channel].writable ? O_WRONLY : O_RDONLY;
		strbuf_addf(&path, "%s/%s", transport->config.location,
			    transport->config.channels[channel].name);
		transport->fds[channel] = open(path.buf, mode);
		strbuf_release(&path);
		if (transport->fds[channel] < 0)
			return PRELOAD_TRANSPORT_SYSTEM_ERROR;
	}
	return PRELOAD_TRANSPORT_OK;
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
	return preload_connection_read(transport->fds[channel], buffer, size);
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
	return preload_connection_write(transport->fds[channel], buffer, size);
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
	if (transport->fds[channel] < 0) {
		errno = EBADF;
		return -1;
	}
	return 0;
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
	int fd = transport->fds[channel];
	transport->fds[channel] = -1;
	return fd;
}

/**
 * @param transport Borrowed prepared or connected state.
 * @param channel Configured zero-based channel index.
 * @return Nonzero if selected and still owned, zero before connection or after transfer/close.
 */
int preload_transport_has_channel(struct preload_transport *transport, size_t channel)
{
	return transport->fds[channel] >= 0;
}

/**
 * @brief Close one channel after connection work has finished.
 *
 * @param transport Borrowed connected state; retains all other channels.
 * @param channel Configured zero-based index; closing an already closed channel is safe.
 */
void preload_transport_close_channel(struct preload_transport *transport, size_t channel)
{
	if (transport->fds[channel] >= 0)
		close(transport->fds[channel]);
	transport->fds[channel] = -1;
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
	for (channel = 0; channel < transport->config.nr; channel++)
		preload_transport_close_channel(transport, channel);
	free(transport->fds);
	free(transport);
}
