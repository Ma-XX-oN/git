#ifndef PRELOAD_TRANSPORT_H
#define PRELOAD_TRANSPORT_H

#include "git-compat-util.h"

/**
 * @file
 * Blocking preload channels. start prepares endpoints before READY; wait
 * connects them in the supplied order. The transport owns all channels until
 * detach_fd transfers one descriptor, close_channel closes one, or release
 * cancels/joins connection operations and closes the remaining channels.
 *
 * Configuration and channel strings are borrowed until release. Windows uses
 * synchronous handles and two blocking connector threads, never OVERLAPPED.
 * POSIX opens FIFOs already created by the owning manager.
 */
struct preload_transport;

enum preload_transport_result {
	PRELOAD_TRANSPORT_OK,
	PRELOAD_TRANSPORT_TIMEOUT,
	PRELOAD_TRANSPORT_INVALID_MARKER,
	PRELOAD_TRANSPORT_DUPLICATE_MARKER,
	PRELOAD_TRANSPORT_SYSTEM_ERROR
};

struct preload_channel_spec {
	const char *name;
	int writable; /* Application-data direction, relative to the server. */
};

struct preload_transport_config {
	const char *location; /* Windows pipe-name stem or existing FIFO directory. */
	const struct preload_channel_spec *channels;
	const size_t *order;
	size_t nr;
	unsigned int timeout_ms;
	int expected_marker; /* ASCII digit, or zero to learn the worker's digit. */
};

/** @brief Prepare endpoints and connection state; caller releases even on error. */
int preload_transport_start(struct preload_transport **transport,
			    const struct preload_transport_config *config);
/** @brief Finish connections, returning a preload_transport_result. */
int preload_transport_wait(struct preload_transport *transport);
/** @brief Read available application bytes; return zero for EOF or -1 on error. */
ssize_t preload_transport_read(struct preload_transport *transport, size_t channel,
			       void *buffer, size_t size);
/** @brief Write application bytes, returning bytes written or -1 on error. */
ssize_t preload_transport_write(struct preload_transport *transport, size_t channel,
				const void *buffer, size_t size);
/** @brief Finish buffered delivery where the platform requires it. */
int preload_transport_flush(struct preload_transport *transport, size_t channel);
/** @brief Transfer a connected channel to an owned CRT/POSIX descriptor. */
int preload_transport_detach_fd(struct preload_transport *transport, size_t channel);
/** @brief Return whether a connected channel is still owned by the transport. */
int preload_transport_has_channel(struct preload_transport *transport, size_t channel);
/** @brief Close one channel still owned by the transport; repeated closes are safe. */
void preload_transport_close_channel(struct preload_transport *transport, size_t channel);
/** @brief Cancel/join pending connections and release all owned resources. */
void preload_transport_release(struct preload_transport *transport);

#endif
