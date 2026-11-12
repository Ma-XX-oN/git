#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include "manager/platform.hpp"
#include <cstdlib>
#include <chrono>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>
#include <cerrno>
#include <limits>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

namespace preload {
/** Convert borrowed UTF-8 to owned UTF-16; throw on invalid encoding. */
static std::wstring wide(const std::string &text)
{
	int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), NULL, 0);
	if (!size && !text.empty()) throw std::runtime_error("invalid UTF-8 path");
	std::wstring result(size, L'\0');
	if (size) MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), result.data(), size);
	return result;
}

/** Quote borrowed argument using the Windows CRT backslash/quote convention. */
static std::wstring quote(const std::wstring &text)
{
	std::wstring result = L"\"";
	unsigned slashes = 0;
	for (auto c : text) {
		if (c == L'\\') { ++slashes; continue; }
		result.append(c == L'"' ? slashes * 2 + 1 : slashes, L'\\');
		slashes = 0;
		result += c;
	}
	result.append(slashes * 2, L'\\');
	return result + L'"';
}

/** Owned native handle, closed on destruction, never copied. */
struct Handle {
	HANDLE value = NULL;
	~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
	Handle() = default;
	Handle(const Handle &) = delete;
	Handle &operator=(const Handle &) = delete;
};

struct Process::Impl {
	Handle process, output;
	std::mutex mutex;
};

Process::Process(const std::string &executable, const std::string &directory) : impl(new Impl)
{
	SECURITY_ATTRIBUTES security{sizeof(security), NULL, TRUE};
	Handle writer, input;
	if (!CreatePipe(&impl->output.value, &writer.value, &security, 0) ||
	    !SetHandleInformation(impl->output.value, HANDLE_FLAG_INHERIT, 0))
		throw std::runtime_error("CreatePipe(READY) failed");
	input.value = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
		&security, OPEN_EXISTING, 0, NULL);
	if (input.value == INVALID_HANDLE_VALUE) throw std::runtime_error("open NUL failed");
	STARTUPINFOEXW startup{};
	startup.StartupInfo.cb = sizeof(startup);
	startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
	startup.StartupInfo.hStdInput = input.value;
	startup.StartupInfo.hStdOutput = writer.value;
	startup.StartupInfo.hStdError = input.value;
	SIZE_T size = 0;
	InitializeProcThreadAttributeList(NULL, 1, 0, &size);
	std::vector<unsigned char> storage(size);
	startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());
	if (!InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &size))
		throw std::runtime_error("initialize handle whitelist failed");
	HANDLE inherited[] = {input.value, writer.value};
	bool configured = UpdateProcThreadAttribute(startup.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
		inherited, sizeof(inherited), NULL, NULL);
	auto path = wide(executable);
	auto command = quote(path) + L" --preload-worker " + quote(wide(directory));
	PROCESS_INFORMATION process{};
	bool created = configured && CreateProcessW(path.c_str(), command.data(), NULL, NULL, TRUE,
		EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW, NULL, NULL, &startup.StartupInfo, &process);
	DWORD error = GetLastError();
	DeleteProcThreadAttributeList(startup.lpAttributeList);
	if (!created) throw std::runtime_error("CreateProcess failed: " + std::to_string(error));
	impl->process.value = process.hProcess;
	CloseHandle(process.hThread);
	pid = process.dwProcessId;
}

Process::~Process() { preload_connection_close(context); }

bool Process::ready(unsigned timeout_ms)
{
	std::string line;
	auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
	while (std::chrono::steady_clock::now() < deadline && line.size() < 64) {
		DWORD available, count;
		if (!PeekNamedPipe(impl->output.value, NULL, 0, NULL, &available, NULL)) break;
		if (available) {
			char byte;
			if (!ReadFile(impl->output.value, &byte, 1, &count, NULL) || !count) break;
			line += byte;
			if (byte == '\n') break;
		} else {
			if (WaitForSingleObject(impl->process.value, 0) != WAIT_TIMEOUT) break;
			std::this_thread::sleep_for(std::chrono::milliseconds(1));
		}
	}
	CloseHandle(impl->output.value);
	impl->output.value = NULL;
	return line == "READY " + std::to_string(pid) + "\n";
}

bool Process::status(std::uint32_t &code, bool &signal)
{
	std::lock_guard<std::mutex> lock(impl->mutex);
	if (WaitForSingleObject(impl->process.value, 0) == WAIT_TIMEOUT) return false;
	DWORD result;
	if (!GetExitCodeProcess(impl->process.value, &result)) throw std::runtime_error("GetExitCodeProcess failed");
	code = result;
	signal = false;
	return true;
}

void Process::kill()
{
	std::lock_guard<std::mutex> lock(impl->mutex);
	if (WaitForSingleObject(impl->process.value, 0) == WAIT_TIMEOUT) TerminateProcess(impl->process.value, 1);
}

struct Owner::Impl { Handle process; };
Owner::Owner(std::uint32_t pid) : impl(new Impl)
{
	impl->process.value = OpenProcess(SYNCHRONIZE, FALSE, pid);
	if (!impl->process.value) throw std::runtime_error("OpenProcess(owner) failed");
}
Owner::~Owner() = default;
bool Owner::alive() const { return WaitForSingleObject(impl->process.value, 0) == WAIT_TIMEOUT; }
std::uint32_t process_id() { return GetCurrentProcessId(); }
void set_environment(const std::string &name, const std::string &value)
{
	if (!SetEnvironmentVariableW(wide(name).c_str(), wide(value).c_str())) throw std::runtime_error("set environment failed");
}
}

/** Native Unicode entry; convert arguments to UTF-8 for shared option parsing. */
int wmain(int argc, wchar_t **argv)
{
	std::vector<std::string> strings;
	std::vector<char *> arguments;
	for (int i = 0; i < argc; ++i) {
		int size = WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, NULL, 0, NULL, NULL);
		std::string text(size, '\0');
		WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, text.data(), size, NULL, NULL);
		text.pop_back();
		strings.push_back(std::move(text));
	}
	for (auto &text : strings) arguments.push_back(text.data());
	return preload::manager_main(argc, arguments.data());
}
#else
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <signal.h>
#include <sys/wait.h>
#include <cstring>
extern char **environ;

namespace preload {
struct Process::Impl {
	int output = -1;
	std::mutex mutex;
	bool exited = false;
	int status = 0;
	~Impl() { if (output != -1) ::close(output); }
};

Process::Process(const std::string &executable, const std::string &directory) : impl(new Impl)
{
	int descriptors[2];
	if (pipe(descriptors)) throw std::runtime_error("pipe(READY) failed");
	for (auto fd : descriptors) fcntl(fd, F_SETFD, FD_CLOEXEC);
	impl->output = descriptors[0];
	posix_spawn_file_actions_t actions;
	posix_spawn_file_actions_init(&actions);
	posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0);
	posix_spawn_file_actions_adddup2(&actions, descriptors[1], 1);
	posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0);
	char *argv[] = {const_cast<char *>(executable.c_str()), const_cast<char *>("--preload-worker"),
		const_cast<char *>(directory.c_str()), nullptr};
	pid_t child;
	int error = posix_spawn(&child, executable.c_str(), &actions, NULL, argv, environ);
	posix_spawn_file_actions_destroy(&actions);
	::close(descriptors[1]);
	if (error) throw std::runtime_error(std::string("spawn failed: ") + std::strerror(error));
	pid = static_cast<std::uint32_t>(child);
}

Process::~Process() { preload_connection_close(context); }
bool Process::ready(unsigned timeout_ms)
{
	std::string line;
	auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
	while (line.size() < 64) {
		auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
		if (remaining <= 0) break;
		pollfd fd{impl->output, POLLIN, 0};
		int result = poll(&fd, 1, static_cast<int>(remaining));
		if (result < 0 && errno == EINTR) continue;
		if (result <= 0) break;
		char byte;
		if (read(impl->output, &byte, 1) != 1) break;
		line += byte;
		if (byte == '\n') break;
	}
	::close(impl->output);
	impl->output = -1;
	return line == "READY " + std::to_string(pid) + "\n";
}

bool Process::status(std::uint32_t &code, bool &signal)
{
	std::lock_guard<std::mutex> lock(impl->mutex);
	if (!impl->exited) {
		pid_t result;
		do { result = waitpid(static_cast<pid_t>(pid), &impl->status, WNOHANG); } while (result < 0 && errno == EINTR);
		if (!result) return false;
		if (result < 0) throw std::runtime_error("waitpid failed");
		impl->exited = true;
	}
	signal = WIFSIGNALED(impl->status);
	code = signal ? WTERMSIG(impl->status) : WEXITSTATUS(impl->status);
	return true;
}

void Process::kill()
{
	std::lock_guard<std::mutex> lock(impl->mutex);
	if (!impl->exited) ::kill(static_cast<pid_t>(pid), SIGKILL);
}

struct Owner::Impl { std::uint32_t pid; };
Owner::Owner(std::uint32_t pid) : impl(new Impl{pid})
{
	if (pid > static_cast<std::uint32_t>(std::numeric_limits<pid_t>::max()))
		throw std::runtime_error("owner PID outside native range");
}
Owner::~Owner() = default;
bool Owner::alive() const { return !::kill(static_cast<pid_t>(impl->pid), 0) || errno == EPERM; }
std::uint32_t process_id() { return static_cast<std::uint32_t>(getpid()); }
void set_environment(const std::string &name, const std::string &value)
{
	if (setenv(name.c_str(), value.c_str(), 1)) throw std::runtime_error("setenv failed");
}
}
int main(int argc, char **argv) { return preload::manager_main(argc, argv); }
#endif
