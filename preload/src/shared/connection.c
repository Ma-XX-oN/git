/* Git's build force-includes git-compat-util.h, which already selects features. */
#if !defined(_WIN32) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200809L
#endif
#include "shared/connection.h"
#include <errno.h>
#include <limits.h>

#ifdef _WIN32
#include <windows.h>
#include <stdlib.h>

/** Convert borrowed UTF-8 path to owned UTF-16; NULL on invalid input/allocation. */
static wchar_t *wide_path(const char *path)
{
	int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, NULL, 0);
	wchar_t *result = size ? malloc(size * sizeof(*result)) : NULL;
	if (result)
		MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, result, size);
	return result;
}

/** Preserve Win32 error for diagnostics and expose portable I/O failure. */
static int failed(void)
{
	errno = EIO;
	return -1;
}

preload_connection preload_connection_listen(const char *path, int first, unsigned maximum)
{
	HANDLE token = NULL, pipe = INVALID_HANDLE_VALUE;
	TOKEN_USER *user = NULL;
	ACL *acl = NULL;
	DWORD size = 0, acl_size, error;
	SECURITY_DESCRIPTOR descriptor;
	SECURITY_ATTRIBUTES attributes = { sizeof(attributes), &descriptor, FALSE };
	wchar_t *wide = wide_path(path);
	if (!wide || !OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token))
		goto done;
	GetTokenInformation(token, TokenUser, NULL, 0, &size);
	user = malloc(size);
	if (!user || !GetTokenInformation(token, TokenUser, user, size, &size))
		goto done;
	acl_size = sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) - sizeof(DWORD) + GetLengthSid(user->User.Sid);
	acl = malloc(acl_size);
	if (!acl || !InitializeAcl(acl, acl_size, ACL_REVISION) ||
	    !AddAccessAllowedAce(acl, ACL_REVISION, GENERIC_ALL, user->User.Sid) ||
	    !InitializeSecurityDescriptor(&descriptor, SECURITY_DESCRIPTOR_REVISION) ||
	    !SetSecurityDescriptorDacl(&descriptor, TRUE, acl, FALSE))
		goto done;
	pipe = CreateNamedPipeW(wide, PIPE_ACCESS_DUPLEX |
		(first ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0),
		PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
		maximum, 65536, 65536, 5000, &attributes);
done:
	error = GetLastError();
	if (token) CloseHandle(token);
	free(acl);
	free(user);
	free(wide);
	SetLastError(error);
	return (preload_connection)pipe;
}

int preload_connection_accept(preload_connection connection)
{
	return ConnectNamedPipe((HANDLE)connection, NULL) || GetLastError() == ERROR_PIPE_CONNECTED ? 0 : failed();
}

int preload_connection_disconnect(preload_connection connection)
{
	return DisconnectNamedPipe((HANDLE)connection) ? 0 : failed();
}

preload_connection preload_connection_connect(const char *path, unsigned timeout_ms)
{
	wchar_t *wide = wide_path(path);
	HANDLE handle = INVALID_HANDLE_VALUE;
	ULONGLONG deadline = GetTickCount64() + timeout_ms;
	if (!wide) return -1;
	do {
		handle = CreateFileW(wide, GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
		if (handle != INVALID_HANDLE_VALUE) break;
		if (GetLastError() != ERROR_PIPE_BUSY && GetLastError() != ERROR_FILE_NOT_FOUND) break;
		WaitNamedPipeW(wide, 5);
		Sleep(1);
	} while (GetTickCount64() < deadline);
	free(wide);
	return (preload_connection)handle;
}

ptrdiff_t preload_connection_read(preload_connection connection, void *buffer, size_t size)
{
	DWORD count;
	if (ReadFile((HANDLE)connection, buffer, size > MAXDWORD ? MAXDWORD : (DWORD)size, &count, NULL))
		return count;
	return GetLastError() == ERROR_BROKEN_PIPE ? 0 : failed();
}

ptrdiff_t preload_connection_write(preload_connection connection, const void *buffer, size_t size)
{
	DWORD count;
	return WriteFile((HANDLE)connection, buffer, size > MAXDWORD ? MAXDWORD : (DWORD)size, &count, NULL) ?
		(ptrdiff_t)count : failed();
}

int preload_connection_flush(preload_connection connection)
{
	return FlushFileBuffers((HANDLE)connection) ? 0 : failed();
}

void preload_connection_cancel(preload_connection connection)
{
	CancelIoEx((HANDLE)connection, NULL);
}

void preload_connection_close(preload_connection connection)
{
	if (connection != -1) CloseHandle((HANDLE)connection);
}
#else
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>

int preload_connection_mailbox(const char *path, preload_connection *reader, preload_connection *writer)
{
	*reader = *writer = -1;
	if (mkfifo(path, 0600)) return -1;
	*reader = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
	if (*reader == -1) return -1;
	*writer = open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	return *writer == -1 ? -1 : 0;
}

ptrdiff_t preload_connection_read(preload_connection connection, void *buffer, size_t size)
{
	ptrdiff_t result;
	do { result = read((int)connection, buffer, size); } while (result < 0 && errno == EINTR);
	return result;
}

ptrdiff_t preload_connection_write(preload_connection connection, const void *buffer, size_t size)
{
	ptrdiff_t result;
	do { result = write((int)connection, buffer, size); } while (result < 0 && errno == EINTR);
	return result;
}

int preload_connection_flush(preload_connection connection) { return connection == -1 ? -1 : 0; }
/* POSIX manager uses nonblocking descriptors and bounded poll waits. */
void preload_connection_cancel(preload_connection connection) { (void)connection; }
void preload_connection_close(preload_connection connection) { if (connection != -1) close((int)connection); }
#endif
