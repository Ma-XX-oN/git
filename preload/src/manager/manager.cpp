#include "manager/manager.hpp"
#include <algorithm>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <cstdlib>

namespace preload {
void log(const std::string &text)
{
	static std::mutex output;
	std::lock_guard<std::mutex> lock(output);
	std::cout << text << std::endl;
}

std::string pid_frame(std::uint32_t pid)
{
	char buffer[9];
	std::snprintf(buffer, sizeof(buffer), "%08x", static_cast<unsigned>(pid));
	return buffer;
}

bool parse_pid(const std::string &text, std::uint32_t &pid)
{
	std::uint64_t value = 0;
	if (text.empty() || text.size() > 10) return false;
	for (char c : text) {
		if (c < '0' || c > '9') return false;
		value = value * 10 + c - '0';
	}
	if (!value || value > UINT32_MAX) return false;
	pid = static_cast<std::uint32_t>(value);
	return true;
}

Connection &Connection::operator=(Connection &&other) noexcept
{
	if (this != &other) {
		preload_connection_close(value);
		value = other.value;
		other.value = -1;
	}
	return *this;
}

bool Connection::write(const std::string &payload) const
{
	std::size_t sent = 0;
	while (sent < payload.size()) {
		auto count = preload_connection_write(value, payload.data() + sent, payload.size() - sent);
		if (count <= 0) return false;
		sent += static_cast<std::size_t>(count);
	}
	return true;
}

bool Connection::line(std::string &pending, std::string &result) const
{
	for (;;) {
		auto end = pending.find('\n');
		if (end != std::string::npos) {
			if (end > 4096) return false;
			result = pending.substr(0, end);
			pending.erase(0, end + 1);
			return true;
		}
		if (pending.size() > 4096) return false;
		char buffer[4096];
		auto count = preload_connection_read(value, buffer, sizeof(buffer));
		if (count <= 0) return false;
		pending.append(buffer, static_cast<std::size_t>(count));
	}
}

Tasks::Tasks(unsigned count)
{
	if (!count) throw std::invalid_argument("task thread count must be positive");
	try { while (count--) threads.emplace_back([this] { run(); }); }
	catch (...) { close(); throw; }
}

Tasks::~Tasks() { close(); }

void Tasks::submit(std::function<void()> task)
{
	std::lock_guard<std::mutex> lock(mutex);
	if (closing) throw std::logic_error("task submission after shutdown");
	queue.push_back(std::move(task));
	changed.notify_one();
}

void Tasks::run()
{
	for (;;) {
		std::function<void()> task;
		{
			std::unique_lock<std::mutex> lock(mutex);
			changed.wait(lock, [this] { return closing || !queue.empty(); });
			if (queue.empty()) return;
			task = std::move(queue.front());
			queue.pop_front();
		}
		try { task(); }
		catch (const std::exception &error) { log(std::string("TASK_FAILED ") + error.what()); }
	}
}

void Tasks::close()
{
	{ std::lock_guard<std::mutex> lock(mutex); closing = true; }
	changed.notify_all();
	for (auto &thread : threads) if (thread.joinable()) thread.join();
}

void Manager::schedule(const std::shared_ptr<Pool> &pool, unsigned timeout_ms)
{
	if (closing || pool->closing) return;
	++pool->spawning;
	try { spawner.submit([this, pool, timeout_ms] { spawn(pool, timeout_ms); }); }
	catch (...) { --pool->spawning; throw; }
}

void Manager::spawn(const std::shared_ptr<Pool> &pool, unsigned timeout_ms)
{
	Worker worker;
	std::string error;
	try {
		{ std::lock_guard<std::mutex> lock(mutex);
			if (closing || pool->closing) throw std::runtime_error("manager stopping"); }
		worker = std::make_shared<Process>(pool->executable, directory);
		{ std::lock_guard<std::mutex> lock(mutex);
			pool->children.push_back(worker);
			if (closing || pool->closing) worker->kill(); }
		if (!worker->ready(timeout_ms)) throw std::runtime_error("no READY from pid=" + std::to_string(worker->pid));
#ifdef _WIN32
		const char *mode = std::getenv("EXP_PRELOAD_CONNECT");
		if (mode && std::string(mode) == "spawn") {
			worker->context = preload_connection_connect((pipe_directory + "\\worker-" +
				std::to_string(worker->pid) + "\\params-cwd").c_str(), 200);
			log(std::string(worker->context == -1 ? "WORKER_PREPARE_FAILED " : "CONTEXT_PRECONNECTED ") + std::to_string(worker->pid));
		}
#endif
	} catch (const std::exception &failure) { error = failure.what(); }
	std::lock_guard<std::mutex> lock(mutex);
	if (!error.empty() || closing || pool->closing) {
		if (worker) { worker->kill(); retired.push_back(worker); }
		pool->children.erase(std::remove(pool->children.begin(), pool->children.end(), worker), pool->children.end());
		pool->error = error.empty() ? "manager stopping" : error;
		log("WORKER_NOT_READY " + pool->name + " " + pool->error);
	} else {
		pool->ready.push_back(worker);
		log("WORKER_READY " + pool->name + " " + std::to_string(worker->pid));
	}
	--pool->spawning;
	changed.notify_all();
}

void Manager::close_pool(const std::shared_ptr<Pool> &pool)
{
	std::unique_lock<std::mutex> lock(mutex);
	pool->closing = true;
	for (auto &worker : pool->children) worker->kill();
	changed.notify_all();
	changed.wait(lock, [&] { return pool->spawning == 0; });
	retired.insert(retired.end(), pool->children.begin(), pool->children.end());
	pool->children.clear();
	pool->ready.clear();
}

std::string Manager::preload(const std::string &name, const std::string &executable)
{
	if (name.empty() || executable.empty()) return "ERROR usage";
	std::error_code ec;
	if (!std::filesystem::is_regular_file(std::filesystem::u8path(executable), ec))
		return "ERROR executable not found: " + executable;
	auto pool = std::make_shared<Pool>();
	pool->name = name;
	pool->executable = executable;
	bool ready;
	{
		std::unique_lock<std::mutex> lock(mutex);
		if (closing) return "ERROR manager stopping";
		for (unsigned i = 0; i < 3; ++i) schedule(pool, 1000);
		changed.wait(lock, [&] { return !pool->spawning; });
		ready = !closing && pool->ready.size() == 3 && pool->error.empty();
	}
	if (!ready) { close_pool(pool); return "ERROR " + name + " doesn't support --preload-worker"; }
	std::shared_ptr<Pool> previous;
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (closing) { pool->closing = true; }
		else { previous = pools[name]; pools[name] = pool; }
	}
	if (pool->closing) { close_pool(pool); return "ERROR manager stopping"; }
	if (previous) close_pool(previous);
	log("POOL_READY " + name + " " + executable);
	return "OK";
}

Worker Manager::allocate(const std::string &name, std::string &error, bool wait)
{
	std::unique_lock<std::mutex> lock(mutex);
	auto found = pools.find(name);
	if (found == pools.end()) { error = "ERROR not preloaded: " + name; return {}; }
	auto pool = found->second;
	bool grown = false;
	while (!closing && !pool->closing) {
		while (!pool->ready.empty()) {
			auto worker = pool->ready.front();
			pool->ready.pop_front();
			pool->children.erase(std::remove(pool->children.begin(), pool->children.end(), worker), pool->children.end());
			std::uint32_t code; bool signal;
			if (!worker->status(code, signal)) {
				allocated[worker->pid] = worker;
				log("ALLOCATED " + name + " " + std::to_string(worker->pid));
				return worker;
			}
			schedule(pool);
		}
		if (!wait) return {};
		if (!grown) {
			if (pool->target < 32) { ++pool->target; schedule(pool); log("POOL_GROW " + name + " target=" + std::to_string(pool->target)); }
			grown = true;
		} else if (!pool->error.empty() && !pool->spawning) {
			error = "ERROR workers failing for " + name + ": " + pool->error;
			return {};
		}
		changed.wait_for(lock, std::chrono::milliseconds(500));
	}
	error = "ERROR manager stopping";
	return {};
}

void Manager::replenish(const std::string &name)
{
	std::lock_guard<std::mutex> lock(mutex);
	auto found = pools.find(name);
	if (found != pools.end()) schedule(found->second);
}

std::string Manager::finish(const std::string &text, bool report)
{
	std::uint32_t pid;
	if (!parse_pid(text, pid)) return report ? "ERROR invalid worker PID" : "";
	Worker worker;
	{
		std::lock_guard<std::mutex> lock(mutex);
		auto found = allocated.find(pid);
		if (found == allocated.end()) return report ? "ERROR unknown worker " + text : "";
		worker = found->second;
		allocated.erase(found);
		retired.push_back(worker);
	}
	if (!report) return "";
	auto deadline = Clock::now() + std::chrono::seconds(10);
	std::uint32_t code; bool signal;
	do {
		if (worker->status(code, signal)) {
			log("WORKER_LOST " + text + " returncode=" + (signal ? "-" : "") + std::to_string(code));
			return std::string(signal ? "SIGNAL " : "EXIT ") + std::to_string(code);
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(1));
	} while (Clock::now() < deadline);
	return "ERROR worker still running " + text;
}

void Manager::reap()
{
	std::lock_guard<std::mutex> lock(mutex);
	retired.erase(std::remove_if(retired.begin(), retired.end(), [](const Worker &worker) {
		std::uint32_t code; bool signal; return worker->status(code, signal);
	}), retired.end());
}

void Manager::stop()
{
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (closing) return;
		closing = true;
		for (auto &entry : pools) for (auto &worker : entry.second->children) worker->kill();
		for (auto &entry : allocated) entry.second->kill();
		for (auto &worker : retired) worker->kill();
	}
	changed.notify_all();
}

void Manager::close()
{
	if (closed) return;
	stop();
	spawner.close();
	for (auto &entry : pools) close_pool(entry.second);
	for (auto &entry : allocated) retired.push_back(entry.second);
	allocated.clear();
	for (auto &worker : retired) {
		worker->kill();
		std::uint32_t code; bool signal;
		while (!worker->status(code, signal)) std::this_thread::sleep_for(std::chrono::milliseconds(1));
	}
	retired.clear();
	closed = true;
}

Manager::~Manager() { close(); }
}
