#ifndef PRELOAD_TRANSPORT_H
#define PRELOAD_TRANSPORT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @file
 * Blocking preload channels. start prepares endpoints before READY; wait
 * connects them in the supplied order. The transport owns all channels until
 * detach_fd transfers one descriptor, close_channel closes one, or release
 * cancels/joins connection operations and closes the remaining channels.
 *
 * Configuration and channel strings are borrowed until release. Windows uses
 * synchronous handles and two blocking connector threads.
 * POSIX opens FIFOs created during worker endpoint preparation.
 */
struct preload_transport;

enum preload_transport_result {
	PRELOAD_TRANSPORT_OK,
	PRELOAD_TRANSPORT_TIMEOUT,
	PRELOAD_TRANSPORT_INVALID_MARKER,
	PRELOAD_TRANSPORT_DUPLICATE_MARKER,
	PRELOAD_TRANSPORT_SYSTEM_ERROR
};

/** A logical application's channel name and server-side direction. */
struct preload_channel_spec {
	const char *name; /**< Borrowed endpoint suffix, valid until release. */
	int writable; /**< Nonzero for server output, zero for server input. */
};

/** Borrowed connection configuration copied by preload_transport_start(). */
struct preload_transport_config {
	const char *location; /**< Windows pipe-name stem or existing FIFO directory. */
	const struct preload_channel_spec *channels; /**< Borrowed array of nr specs. */
	const size_t *order; /**< Borrowed permutation of channel indices, length nr. */
	size_t nr; /**< Number of channels, from one through 32. */
	unsigned int timeout_ms; /**< Windows per-channel deadline, 1 through INT_MAX milliseconds. */
	int expected_marker; /**< ASCII digit, or zero to learn the worker's digit. */
};

/**
 * @brief Prepare endpoints and connection state.
 *
 * @param transport Receives owned state, possibly partial on error; always release it.
 * @param config Borrowed configuration; referenced storage must outlive the transport.
 * @return Zero on success, -1 with errno set on failure.
 */
int preload_transport_start(struct preload_transport **transport,
			    const struct preload_transport_config *config);
/**
 * @brief Complete connections in the configured order before application I/O.
 *
 * @param transport Borrowed prepared state; invoke once before transferring channels.
 * @return A preload_transport_result; failed connections remain owned until release.
 */
int preload_transport_wait(struct preload_transport *transport);
/**
 * @brief Read application bytes from a connected input channel.
 *
 * @param transport Borrowed connected state.
 * @param channel Configured zero-based channel index, still owned by transport.
 * @param buffer Caller-owned destination with at least size writable bytes.
 * @param size Maximum byte count to read.
 * @return Bytes read, zero at EOF, or -1 with errno set; short reads are valid.
 */
ptrdiff_t preload_transport_read(struct preload_transport *transport, size_t channel,
			       void *buffer, size_t size);
/**
 * @brief Write application bytes to a connected output channel.
 *
 * @param transport Borrowed connected state.
 * @param channel Configured zero-based channel index, still owned by transport.
 * @param buffer Borrowed source with at least size readable bytes.
 * @param size Maximum byte count to write.
 * @return Bytes written, or -1 with errno set; callers handle short writes.
 */
ptrdiff_t preload_transport_write(struct preload_transport *transport, size_t channel,
				const void *buffer, size_t size);
/**
 * @brief Finish buffered delivery where the platform requires it.
 *
 * @param transport Borrowed connected state.
 * @param channel Configured output channel index, still owned by transport.
 * @return Zero on success, -1 with errno set on failure. Windows waits for consumption.
 */
int preload_transport_flush(struct preload_transport *transport, size_t channel);
/**
 * @brief Transfer a connected channel to a caller-owned CRT/POSIX descriptor.
 *
 * @param transport Borrowed connected state; relinquishes the selected channel.
 * @param channel Configured channel index, still owned by transport.
 * @return Descriptor on success, -1 on failure, retaining ownership on failure.
 */
int preload_transport_detach_fd(struct preload_transport *transport, size_t channel);
/**
 * @param transport Borrowed prepared or connected state.
 * @param channel Configured zero-based channel index.
 * @return Nonzero if selected and still owned, zero before connection or after transfer/close.
 */
int preload_transport_has_channel(struct preload_transport *transport, size_t channel);
/**
 * @brief Close one channel after connection work has finished.
 *
 * @param transport Borrowed connected state; retains all other channels.
 * @param channel Configured zero-based index; closing an already closed channel is safe.
 */
void preload_transport_close_channel(struct preload_transport *transport, size_t channel);
/**
 * @brief Cancel/join pending connections and release all owned resources.
 *
 * @param transport Owned prepared or connected state, including partial startup; NULL is safe.
 */
void preload_transport_release(struct preload_transport *transport);

#ifdef __cplusplus
}
#endif

#endif
