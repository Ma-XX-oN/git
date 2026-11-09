#include "git-compat-util.h"
#include "preload-transport.h"
#include "strbuf.h"

struct preload_transport {
	struct preload_transport_config config;
	int *fds;
};

/** @brief Prepare borrowed FIFO names; their manager owns creation and removal. */
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

/** @brief Open each unidirectional FIFO in the established client/server order. */
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

/** @brief Read application bytes, retrying interrupted FIFO reads. */
ssize_t preload_transport_read(struct preload_transport *transport, size_t channel,
			       void *buffer, size_t size)
{
	return xread(transport->fds[channel], buffer, size);
}

/** @brief Write application bytes, retrying interrupted FIFO writes. */
ssize_t preload_transport_write(struct preload_transport *transport, size_t channel,
				const void *buffer, size_t size)
{
	return xwrite(transport->fds[channel], buffer, size);
}

/** @brief FIFO writes have no additional userspace transport buffer to flush. */
int preload_transport_flush(struct preload_transport *transport, size_t channel)
{
	if (transport->fds[channel] < 0) {
		errno = EBADF;
		return -1;
	}
	return 0;
}

/** @brief Transfer one FIFO descriptor and remove it from transport ownership. */
int preload_transport_detach_fd(struct preload_transport *transport, size_t channel)
{
	int fd = transport->fds[channel];
	transport->fds[channel] = -1;
	return fd;
}

/** @brief Close a descriptor owned by the transport, including error cleanup. */
int preload_transport_has_channel(struct preload_transport *transport, size_t channel)
{
	return transport->fds[channel] >= 0;
}

/** @brief Close a descriptor owned by the transport, including error cleanup. */
void preload_transport_close_channel(struct preload_transport *transport, size_t channel)
{
	if (transport->fds[channel] >= 0)
		close(transport->fds[channel]);
	transport->fds[channel] = -1;
}

/** @brief Close all descriptors still owned by the transport. */
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
