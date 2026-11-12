#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include "manager/manager.hpp"
#include <algorithm>
#include <array>
#include <fstream>
#include <list>
#include <sstream>
#include <stdexcept>
#include <cstdlib>
#include <cstdio>
#include <utility>
#include <exception>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <poll.h>
#include <unistd.h>
#include <signal.h>
#include <cerrno>
#endif

namespace preload {
/** Split at the first separator, leaving path/argument remainder intact. */
static std::pair<std::string, std::string> split(const std::string &text)
{
	auto space = text.find(' ');
	if (space == std::string::npos) return {text, ""};
	return {text.substr(0, space), text.substr(space + 1)};
}

/** Wall-clock seconds for existing stage diagnostics; never used for deadlines. */
static double timestamp()
{
	return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
}

/** Emit existing GET timing fields only when PRELOAD_TIMING was enabled at startup. */
static void timing(double received, double allocated, double delivered)
{
	static const bool enabled = std::getenv("PRELOAD_TIMING") != nullptr;
	if (!enabled) return;
	char line[256];
	std::snprintf(line, sizeof(line), "TIMING GET received=%.6f thread=%.6f allocated=%.6f delivered=%.6f",
		received, received, allocated, delivered);
	log(line);
}

#ifdef _WIN32

/** Incremental worker-frame boundary scanner; no argument payload is retained. */
class Scanner {
	unsigned count_bytes = 0;
	std::uint64_t strings = 0;
	std::uint32_t count = 0;
	bool value = false;
	std::string tag; /**< At most four bytes distinguish end from other tags. */
public:
	/** Consume borrowed chunk; return bytes through end, or size if incomplete. */
	std::size_t scan(const std::string &chunk, bool &complete)
	{
		for (std::size_t i = 0; i < chunk.size(); ++i) {
			unsigned char c = chunk[i];
			if (count_bytes < 4) {
				count |= std::uint32_t(c) << (8 * count_bytes++);
				if (count_bytes == 4) strings = std::uint64_t(count) + 1;
			} else if (strings) { if (!c) --strings; }
			else if (value) { if (!c) value = false; }
			else if (!c) {
				if (tag == "end") { complete = true; return i + 1; }
				tag.clear(); value = true;
			} else if (tag.size() < 4) tag += c;
		}
		return chunk.size();
	}
};

/** Windows endpoint with reusable listeners and joined per-connection threads. */
class WindowsServer final : public Server {
	/** Ensure failed requests kill undisclosed/stranded workers and always end tracking. */
	struct Call {
		Manager &manager; /**< Borrowed until server threads are joined. */
		Worker worker; /**< Allocated worker retained through request completion. */
		bool complete = false; /**< True only after orderly connection handling. */
		~Call()
		{
			if (!worker) return;
			if (!complete) worker->kill();
			manager.finish(std::to_string(worker->pid));
		}
	};
	/** Kill before joining a blocked relay writer when stack unwinding begins. */
	struct RelayFailure {
		Worker worker;
		int exceptions = std::uncaught_exceptions();
		~RelayFailure() { if (std::uncaught_exceptions() > exceptions) worker->kill(); }
	};
	/** Owned pipe and serving thread, kept stable until done and joined. */
	struct Session {
		Connection pipe;
		std::thread thread;
		std::atomic<bool> done{false};
		std::atomic<DWORD> thread_id{0};
	};
	Manager &manager;
	std::string pipe_directory, address;
	std::mutex mutex;
	std::list<std::unique_ptr<Session>> sessions;
	unsigned listening = 0;
	bool first = true;
	std::atomic<bool> stopped{false};

	/** Create listener under mutex; its stable session is owned until joined. */
	void listen()
	{
		auto session = std::make_unique<Session>();
		session->pipe = Connection(preload_connection_listen(address.c_str(), first, 255));
		if (session->pipe.value == -1) throw std::runtime_error("create request pipe failed: " + std::to_string(GetLastError()));
		first = false;
		auto pointer = session.get();
		sessions.push_back(std::move(session));
		++listening;
		try { pointer->thread = std::thread([this, pointer] { run(*pointer); }); }
		catch (...) { --listening; sessions.pop_back(); throw; }
	}

	/** Relay bounded chunks asynchronously so reading never waits on worker opens.
	 * Returns false on malformed/incomplete input or delivery failure; caller kills worker.
	 */
	bool forward(Session &session, const Worker &worker, char marker, std::string &pending, Connection &context)
	{
		context = Connection(std::exchange(worker->context, -1));
		if (context.value == -1) context = Connection(preload_connection_connect((pipe_directory + "\\worker-" +
			std::to_string(worker->pid) + "\\params-cwd").c_str(), 200));
		if (context.value == -1) return false;
		std::atomic<bool> failed{false};
		std::unique_ptr<Tasks> writer;
		RelayFailure cleanup{worker};
		Scanner scanner;
		bool complete = false;
		std::size_t received = 0;
		if (!context.write(std::string(1, marker))) return false;
		while (!complete && !stopped && !failed) {
			if (pending.empty()) {
				char buffer[4096];
				auto count = preload_connection_read(session.pipe.value, buffer, sizeof(buffer));
				if (count <= 0) break;
				pending.assign(buffer, static_cast<std::size_t>(count));
			}
			auto count = scanner.scan(pending, complete);
			if (received + count > 128 * 1024) { complete = false; break; }
			auto chunk = pending.substr(0, count);
			pending.erase(0, count);
			/* The first <=4096-byte read fits the empty 64 KiB context buffer.
			 * Larger requests need a writer so client reads never wait on worker I/O.
			 */
			if (!received && complete) {
				if (!context.write(chunk)) failed = true;
			} else {
				if (!writer) writer = std::make_unique<Tasks>(1);
				writer->submit([&, chunk = std::move(chunk)] { if (!failed && !context.write(chunk)) failed = true; });
			}
			received += count;
		}
		if (!complete || stopped || failed) worker->kill();
		if (writer) writer->close();
		if (!complete || failed || stopped) return false;
		return !stopped;
	}

	/** Serve one private request and retain GET until caller closes it. */
	void request(Session &session)
	{
		std::string pending, line;
		if (!session.pipe.line(pending, line)) return;
		double received = timestamp(), allocated = received;
		auto command = split(line);
		auto arguments = split(command.second);
		Call call{manager, {}, false};
		auto &worker = call.worker;
		std::string reply;
		if (command.first == "GET") {
			if (!arguments.second.empty() && (arguments.second.size() != 1 || arguments.second[0] < '0' || arguments.second[0] > '9'))
				reply = "00000000ERROR invalid marker\n";
			else {
				worker = manager.allocate(arguments.first, reply);
				allocated = timestamp();
				if (worker && arguments.second.empty()) {
					preload_connection_close(std::exchange(worker->context, -1));
				}
				reply = worker ? pid_frame(worker->pid) : "00000000" + reply + "\n";
			}
		} else if (command.first == "PRELOAD") reply = manager.preload(arguments.first, arguments.second) + "\n";
		else if (line == "STOP") reply = "OK\n";
		else reply = "ERROR unknown request\n";
		bool delivered = session.pipe.write(reply);
		if (command.first == "GET") timing(received, allocated, timestamp());
		if (worker) manager.replenish(arguments.first);
		if (!delivered) {
			return;
		}
		if (line == "STOP") { log("MANAGER_STOP_REQUEST"); stopped = true; }
		if (worker) {
			Connection context;
			bool forwarded = arguments.second.empty() || forward(session, worker, arguments.second[0], pending, context);
			if (!forwarded) worker->kill();
			else while (session.pipe.line(pending, line)) {
				auto status = line == "STATUS" ? manager.finish(std::to_string(worker->pid), true) : "ERROR unknown request";
				if (!session.pipe.write(status + "\n")) break;
			}
			manager.finish(std::to_string(worker->pid));
			/* Closing GET ends tracking; the context lease survives until process exit.
			 * STATUS remains available while the process is still running.
			 */
			if (context.value != -1) {
				std::uint32_t code; bool signal;
				while (!worker->status(code, signal) && !stopped)
					std::this_thread::sleep_for(std::chrono::milliseconds(1));
			}
		} else {
			char discard[4096];
			while (preload_connection_read(session.pipe.value, discard, sizeof(discard)) > 0) {}
		}
		call.complete = true;
	}

	/** Reaccept on this instance while useful; publish completion before reclamation. */
	void run(Session &session)
	{
		session.thread_id = GetCurrentThreadId();
		try {
			while (!stopped) {
				if (preload_connection_accept(session.pipe.value)) break;
				{
					std::lock_guard<std::mutex> lock(mutex);
					--listening;
					while (!stopped && listening < 3) listen();
				}
				try { request(session); }
				catch (const std::exception &error) { log(std::string("REQUEST_FAILED ") + error.what()); }
				preload_connection_disconnect(session.pipe.value);
				std::lock_guard<std::mutex> lock(mutex);
				if (stopped || listening >= 6) break;
				++listening;
			}
		} catch (const std::exception &error) { log(std::string("LISTENER_FAILED ") + error.what()); stopped = true; }
		session.done = true;
	}
public:
	WindowsServer(Manager &manager, std::string pipe_directory) : manager(manager),
		pipe_directory(std::move(pipe_directory)), address(this->pipe_directory + "\\request")
	{
		try { std::lock_guard<std::mutex> lock(mutex); while (listening < 3) listen(); }
		catch (...) { shutdown(); throw; }
	}
	/** Cancel synchronous I/O repeatedly to cover cancellation/submission races, then join. */
	void shutdown()
	{
		stopped = true;
		manager.stop();
		for (;;) {
			bool done = true;
			{
				std::lock_guard<std::mutex> lock(mutex);
				for (auto &session : sessions) if (!session->done) {
					done = false;
					preload_connection_cancel(session->pipe.value);
					HANDLE thread = OpenThread(THREAD_TERMINATE, FALSE, session->thread_id);
					if (thread) { CancelSynchronousIo(thread); CloseHandle(thread); }
				}
			}
			if (done) break;
			std::this_thread::sleep_for(std::chrono::milliseconds(1));
		}
		for (auto &session : sessions) if (session->thread.joinable()) session->thread.join();
		sessions.clear();
	}
	~WindowsServer() override { shutdown(); }
	bool tick() override
	{
		std::this_thread::sleep_for(std::chrono::milliseconds(25));
		std::lock_guard<std::mutex> lock(mutex);
		for (auto it = sessions.begin(); it != sessions.end();) {
			if ((*it)->done) { (*it)->thread.join(); it = sessions.erase(it); }
			else ++it;
		}
		return !stopped;
	}
};

std::unique_ptr<Server> serve(Manager &manager, const std::string &, const std::string &pipe_directory)
{
	return std::make_unique<WindowsServer>(manager, pipe_directory);
}
#else

/** POSIX mailbox state; serial executor preserves order, mutex protects delivery/reclaim. */
struct Mailbox {
	Connection reader, writer;
	std::string path, lock_path;
	std::mutex mutex, dispatch;
	Worker pending;
	unsigned generation = 0;
	std::atomic<unsigned> outstanding{0};
	Tasks tasks{1};
	/** Prepare FIFO; path borrowed only during construction. Throws on failure. */
	explicit Mailbox(std::string path) : path(std::move(path)), lock_path(this->path + ".lock")
	{
		if (preload_connection_mailbox(this->path.c_str(), &reader.value, &writer.value))
			throw std::runtime_error("create mailbox failed");
	}
	/** Read claiming PID; zero means absent/incomplete lock. */
	std::uint32_t owner()
	{
		std::ifstream file(lock_path);
		std::string text; file >> text;
		std::uint32_t pid = 0;
		parse_pid(text, pid);
		return pid;
	}
	/** Drain stale bytes and free claim; kill worker only if its reply was unread. */
	void release()
	{
		std::lock_guard<std::mutex> guard(mutex);
		char bytes[4096];
		bool unread = false;
		while (preload_connection_read(reader.value, bytes, sizeof(bytes)) > 0) unread = true;
		if (unread && pending) { log("UNCOLLECTED_WORKER_KILLED pid=" + std::to_string(pending->pid)); pending->kill(); }
		pending.reset();
		++generation;
		std::error_code error;
		std::filesystem::remove(lock_path, error);
	}
	/** Deliver only to original claim; caller supplies generation captured at submission. */
	bool deliver(const std::string &reply, unsigned claim, Worker worker = {})
	{
		std::lock_guard<std::mutex> guard(mutex);
		if (claim != generation || !std::filesystem::exists(lock_path)) return false;
		if (!writer.write(reply + (worker ? "" : "\n"))) return false;
		pending = std::move(worker);
		return true;
	}
};

class PosixServer final : public Server {
	Manager &manager;
	Connection request, guard;
	std::array<std::unique_ptr<Mailbox>, 32> slots;
	std::string incoming;
	int stop_slot = -1;
	Clock::time_point deadline{}, next_reclaim{};
	bool stopped = false;

	/** Validate mailbox number, including slot zero; null on malformed input. */
	Mailbox *mailbox(const std::string &text)
	{
		if (text.empty() || text.size() > 2) return nullptr;
		unsigned slot = 0;
		for (char c : text) { if (c < '0' || c > '9') return nullptr; slot = slot * 10 + c - '0'; }
		return slot < slots.size() ? slots[slot].get() : nullptr;
	}
	/** Allocation fast path; false means enqueue a blocking attempt on this mailbox. */
	bool get(const std::string &name, Mailbox &box, unsigned claim, bool wait)
	{
		std::string error;
		double received = timestamp();
		auto worker = manager.allocate(name, error, wait);
		double allocated = timestamp();
		if (!worker && error.empty()) return false;
		if (worker) {
			if (!box.deliver(pid_frame(worker->pid), claim, worker)) { worker->kill(); manager.finish(std::to_string(worker->pid)); }
			manager.replenish(name);
		} else box.deliver("00000000" + error, claim);
		timing(received, allocated, timestamp());
		return true;
	}
	/** Dispatch one bounded request line, retaining the executable remainder verbatim. */
	void handle(const std::string &line)
	{
		auto command = split(line);
		auto arg = split(command.second);
		auto rest = split(arg.second);
		if (command.first == "FREE" && arg.second.empty()) {
			if (auto box = mailbox(arg.first)) {
				box->release();
				if (stop_slot >= 0 && box == slots[stop_slot].get()) stopped = true;
			}
			return;
		}
		if (command.first == "DONE" && arg.second.empty()) { manager.finish(arg.first); return; }
		if (command.first == "STOP" && arg.second.empty()) {
			if (auto box = mailbox(arg.first)) {
				log("MANAGER_STOP_REQUEST");
				if (box->deliver("OK", box->generation)) {
					stop_slot = static_cast<int>(std::find_if(slots.begin(), slots.end(), [&](auto &slot) { return slot.get() == box; }) - slots.begin());
					deadline = Clock::now() + std::chrono::seconds(5);
				} else stopped = true;
			}
			return;
		}
		auto box = mailbox(rest.first);
		if (!box) { log("INVALID_REQUEST " + line); return; }
		unsigned claim;
		{ std::lock_guard<std::mutex> lock(box->mutex); claim = box->generation; }
		std::function<void()> task;
		if (command.first == "GET" && rest.second.empty()) {
			std::lock_guard<std::mutex> dispatch(box->dispatch);
			if (!box->outstanding && get(arg.first, *box, claim, false)) return;
			task = [=] { get(arg.first, *box, claim, true); };
		} else if (command.first == "PRELOAD" && !rest.second.empty()) {
			task = [=] { box->deliver(manager.preload(arg.first, rest.second), claim); };
		} else if (command.first == "DONE" && rest.second == "STATUS") {
			task = [=] { box->deliver(manager.finish(arg.first, true), claim); };
		} else { log("INVALID_REQUEST " + line); return; }
		++box->outstanding;
		box->tasks.submit([box, task = std::move(task)] {
			try { task(); } catch (...) { --box->outstanding; throw; }
			--box->outstanding;
		});
	}
public:
	PosixServer(Manager &manager, const std::string &directory) : manager(manager)
	{
		if (preload_connection_mailbox((directory + "/request").c_str(), &request.value, &guard.value))
			throw std::runtime_error("create request FIFO failed");
		for (unsigned i = 0; i < slots.size(); ++i) slots[i] = std::make_unique<Mailbox>(directory + "/slot." + std::to_string(i));
	}
	~PosixServer() override
	{
		manager.stop();
		for (auto &box : slots) if (box) box->tasks.close();
	}
	bool tick() override
	{
		if (stopped || (stop_slot >= 0 && Clock::now() >= deadline)) return false;
		if (Clock::now() >= next_reclaim) {
			for (auto &box : slots) {
				auto pid = box->owner();
				if (pid && ::kill(static_cast<pid_t>(pid), 0) && errno == ESRCH) box->release();
			}
			next_reclaim = Clock::now() + std::chrono::milliseconds(250);
		}
		pollfd fd{static_cast<int>(request.value), POLLIN, 0};
		int result = poll(&fd, 1, 25);
		if (result < 0 && errno != EINTR) throw std::runtime_error("poll(request) failed");
		if (result <= 0) return true;
		char buffer[4096];
		auto count = preload_connection_read(request.value, buffer, sizeof(buffer));
		if (count > 0) incoming.append(buffer, static_cast<std::size_t>(count));
		for (;;) {
			auto end = incoming.find('\n');
			if (end == std::string::npos) break;
			if (end <= 4096) handle(incoming.substr(0, end));
			else log("REQUEST_TOO_LONG");
			incoming.erase(0, end + 1);
		}
		if (incoming.size() > 4096) { log("REQUEST_TOO_LONG"); incoming.clear(); }
		return !stopped;
	}
};

std::unique_ptr<Server> serve(Manager &manager, const std::string &directory, const std::string &)
{
	return std::make_unique<PosixServer>(manager, directory);
}
#endif
}
