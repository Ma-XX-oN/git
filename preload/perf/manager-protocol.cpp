/* Native GET benchmark. Prints JSON samples in ms, excluding worker disposal.
 * Options: directory, pipe-dir, name, count, warmup, threads, duration (seconds),
 * pace (milliseconds). Every option takes one value. Only the READY fixture
 * may be registered: allocated processes are terminated after the timed reply.
 */
#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include "shared/connection.h"
#include <chrono>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <algorithm>
#include <string>
#include <thread>
#include <vector>
#ifdef _WIN32
#include <windows.h>
#else
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#endif

using Clock = std::chrono::steady_clock;

/** Own one native connection, always closing it after the timed transaction. */
struct Connection {
	preload_connection value = -1;
	~Connection() { preload_connection_close(value); }
	/** Write entire borrowed string; throw on failure. */
	void write(const std::string &text)
	{
		std::size_t sent = 0;
		while (sent < text.size()) {
			auto n = preload_connection_write(value, text.data() + sent, text.size() - sent);
			if (n <= 0) throw std::runtime_error("request write failed");
			sent += n;
		}
	}
	/** Read exactly size reply bytes, with a 15 s POSIX wait deadline. */
	std::string read(std::size_t size)
	{
		std::string result(size, '\0');
		std::size_t got = 0;
		while (got < size) {
#ifndef _WIN32
			pollfd fd{static_cast<int>(value), POLLIN, 0};
			if (poll(&fd, 1, 15000) <= 0) throw std::runtime_error("reply timeout");
#endif
			auto n = preload_connection_read(value, result.data() + got, size - got);
			if (n <= 0) throw std::runtime_error("reply read failed");
			got += n;
		}
		return result;
	}
};

/** Execute one allocation; return GET/reply ms, excluding open and worker cleanup. */
static double allocate([[maybe_unused]] const std::string &directory, [[maybe_unused]] const std::string &pipes,
		       const std::string &name, [[maybe_unused]] unsigned slot)
{
	Connection request;
#ifdef _WIN32
	request.value = preload_connection_connect((pipes + "\\request").c_str(), 5000);
	if (request.value == -1) throw std::runtime_error("connect failed");
	auto begin = Clock::now();
	request.write("GET " + name + "\n");
	auto frame = request.read(8);
#else
	request.value = open((directory + "/request").c_str(), O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	Connection reply;
	reply.value = open((directory + "/slot." + std::to_string(slot)).c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
	if (request.value == -1 || reply.value == -1) throw std::runtime_error("open FIFO failed");
	auto begin = Clock::now();
	request.write("GET " + name + " " + std::to_string(slot) + "\n");
	auto frame = reply.read(8);
#endif
	auto end = Clock::now();
	unsigned long pid = std::stoul(frame, nullptr, 16);
	if (!pid) throw std::runtime_error("allocation failed");
#ifdef _WIN32
	HANDLE worker = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, pid);
	if (!worker) throw std::runtime_error("open allocated fixture failed");
	TerminateProcess(worker, 1);
	WaitForSingleObject(worker, 5000);
	CloseHandle(worker);
#else
	kill(static_cast<pid_t>(pid), SIGKILL);
	request.write("DONE " + std::to_string(pid) + "\n");
#endif
	return std::chrono::duration<double, std::milli>(end - begin).count();
}

/** Parse options and run independent client threads; output raw samples only. */
int main(int argc, char **argv)
{
	std::string directory, pipes, name = "probe";
	unsigned count = 500, warmup = 50, threads = 1, duration = 0, pace = 0;
	try {
		for (int i = 1; i + 1 < argc; i += 2) {
			std::string option = argv[i], value = argv[i + 1];
			if (option == "--directory") directory = value;
			else if (option == "--pipe-dir") pipes = value;
			else if (option == "--name") name = value;
			else if (option == "--count") count = std::stoul(value);
			else if (option == "--warmup") warmup = std::stoul(value);
			else if (option == "--threads") threads = std::stoul(value);
			else if (option == "--duration") duration = std::stoul(value);
			else if (option == "--pace") pace = std::stoul(value);
			else throw std::runtime_error("unknown option");
		}
		if (!threads || threads > 32 || !count) throw std::runtime_error("invalid count/threads");
		std::mutex mutex;
		std::vector<double> samples;
		std::vector<std::thread> clients;
		std::atomic<bool> failed{false};
		std::atomic<unsigned> warmed{0};
		std::string failure;
		double measured_wall = 0;
		auto begin = Clock::now();
		for (unsigned slot = 0; slot < threads; ++slot) clients.emplace_back([&, slot] {
			try {
				std::unique_ptr<FILE, int (*)(FILE *)> events(
					std::fopen((directory + "/manager.log").c_str(), "r"), std::fclose);
				if (!events) throw std::runtime_error("cannot open manager.log");
				long available = 0;
				std::string pending; /* Incomplete last line, kept until its newline arrives. */
				/* READY-only samples wait outside timing for three published workers.
				 * Incremental event reads avoid imposing quadratic log-scanning cost.
				 */
				auto await_ready = [&] {
					auto deadline = Clock::now() + std::chrono::seconds(15);
					for (;;) {
						/* The manager appends while we read. C stdio's end-of-file
						 * indicator is sticky on BSD/macOS: without clearerr(), reads
						 * after the first end-of-file return nothing, never seeing new lines.
						 */
						std::clearerr(events.get());
						char buffer[256];
						while (std::fgets(buffer, sizeof(buffer), events.get())) {
							pending += buffer;
							if (pending.back() != '\n') continue;
							if (pending.rfind("WORKER_READY " + name + " ", 0) == 0) ++available;
							if (pending.rfind("ALLOCATED " + name + " ", 0) == 0) --available;
							pending.clear();
						}
						if (available >= 3) return;
						if (Clock::now() >= deadline) throw std::runtime_error("READY queue did not refill");
						std::this_thread::sleep_for(std::chrono::milliseconds(1));
					}
				};
#ifndef _WIN32
				auto lock = directory + "/slot." + std::to_string(slot) + ".lock";
				/* The registering client's FREE is asynchronous: the manager may not
				 * have deleted its lock on this slot yet, so wait for the release.
				 */
				auto claim_deadline = Clock::now() + std::chrono::seconds(5);
				int fd;
				while ((fd = open(lock.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600)) < 0) {
					if (errno != EEXIST || Clock::now() >= claim_deadline)
						throw std::runtime_error("claim failed");
					std::this_thread::sleep_for(std::chrono::milliseconds(1));
				}
				auto pid = std::to_string(getpid());
				::write(fd, pid.data(), pid.size()); ::close(fd);
#endif
				std::vector<double> local;
				for (unsigned i = 0; i < warmup && !failed; ++i) allocate(directory, pipes, name, slot);
				++warmed;
				while (warmed < threads && !failed) std::this_thread::yield();
				auto measured_begin = Clock::now();
				auto deadline = Clock::now() + std::chrono::seconds(duration);
				for (unsigned i = 0; !failed && (duration ? Clock::now() < deadline : i < count); ++i) {
					if (pace) { await_ready(); std::this_thread::sleep_for(std::chrono::milliseconds(pace)); }
					local.push_back(allocate(directory, pipes, name, slot));
				}
#ifndef _WIN32
				Connection request;
				request.value = open((directory + "/request").c_str(), O_WRONLY | O_NONBLOCK);
				request.write("FREE " + std::to_string(slot) + "\n");
#endif
				std::lock_guard<std::mutex> guard(mutex);
				measured_wall = std::max(measured_wall, std::chrono::duration<double>(Clock::now() - measured_begin).count());
				samples.insert(samples.end(), local.begin(), local.end());
			} catch (const std::exception &error) {
				failed = true;
				std::lock_guard<std::mutex> guard(mutex); failure = error.what();
			}
		});
		for (auto &client : clients) client.join();
		if (failed) throw std::runtime_error(failure);
		std::cout << "{\"wall_seconds_including_warmup\":" << std::chrono::duration<double>(Clock::now() - begin).count()
			<< ",\"measured_wall_seconds\":" << measured_wall << ",\"samples_ms\":[";
		for (std::size_t i = 0; i < samples.size(); ++i) std::cout << (i ? "," : "") << samples[i];
		std::cout << "]}\n";
		return 0;
	} catch (const std::exception &error) { std::cerr << error.what() << std::endl; return 1; }
}
