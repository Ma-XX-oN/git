#ifndef PRELOAD_CONNECTION_H
#define PRELOAD_CONNECTION_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Native handle/descriptor owned by its caller; -1 denotes no resource. */
typedef intptr_t preload_connection;

/** Read up to size bytes into borrowed buffer; return count, EOF=0, error=-1. */
ptrdiff_t preload_connection_read(preload_connection connection, void *buffer, size_t size);
/** Write borrowed bytes; return count or -1. Short writes are valid. */
ptrdiff_t preload_connection_write(preload_connection connection, const void *buffer, size_t size);
/** Flush a connected output, waiting for consumption on Windows; return 0/-1. */
int preload_connection_flush(preload_connection connection);
/** Cancel Windows pending operations; POSIX callers use nonblocking I/O and bounded
 * poll waits, so no native cancellation is necessary. Join I/O users before closing.
 */
void preload_connection_cancel(preload_connection connection);
/** Close owned connection; -1 is permitted. No concurrent users may remain. */
void preload_connection_close(preload_connection connection);

#ifdef _WIN32
/** Create owned unmarked duplex server instance at UTF-8 path; -1 on error.
 * first claims the name exclusively. maximum is 2..255; buffers are 64 KiB.
 * Only the current user may access the pipe; remote clients are rejected.
 */
preload_connection preload_connection_listen(const char *path, int first, unsigned maximum);
/** Block accepting one instance; return 0 or -1, including cancellation. */
int preload_connection_accept(preload_connection connection);
/** Disconnect a served instance before reaccepting; return 0 or -1. */
int preload_connection_disconnect(preload_connection connection);
/** Connect to UTF-8 worker channel within timeout_ms; return owned handle/-1. */
preload_connection preload_connection_connect(const char *path, unsigned timeout_ms);
#else
/** Create private FIFO at path and open directional nonblocking reader/writer.
 * Receives owned descriptors (-1 on partial failure); caller closes and unlinks.
 * Return 0/-1. No O_RDWR FIFO extension is used.
 */
int preload_connection_mailbox(const char *path, preload_connection *reader,
			       preload_connection *writer);
#endif

#ifdef __cplusplus
}
#endif
#endif
