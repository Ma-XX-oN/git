#ifndef PRELOAD_MANAGER_HPP
#define PRELOAD_MANAGER_HPP

#include "manager/platform.hpp"
#include "shared/connection.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <functional>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

namespace preload {
using Clock = std::chrono::steady_clock;
using Worker = std::shared_ptr<Process>;

/** Serialize an event line to stdout; borrowed text is copied by the output stream. */
void log(const std::string &text);
/** Format native PID as exactly eight lowercase hexadecimal digits. */
std::string pid_frame(std::uint32_t pid);
/** Parse decimal PID without signs/trailing bytes; return false outside 1..UINT32_MAX. */
bool parse_pid(const std::string &text, std::uint32_t &pid);

/** Move-only native connection. Cancellation requires I/O users to join before destruction. */
class Connection {
public:
	preload_connection value = -1; /**< Owned handle/descriptor, or -1. */
	/** Adopt value (-1 means empty); caller relinquishes ownership. */
	explicit Connection(preload_connection value = -1) : value(value) {}
	/** Close the owned resource; all I/O users must have joined. */
	~Connection() { preload_connection_close(value); }
	Connection(const Connection &) = delete;
	Connection &operator=(const Connection &) = delete;
	/** Transfer ownership from other, leaving it empty. */
	Connection(Connection &&other) noexcept : value(other.value) { other.value = -1; }
	/** Close current resource and adopt other's resource; return this object. */
	Connection &operator=(Connection &&other) noexcept;
	/** Write entire borrowed payload; false on failure/EOF. */
	bool write(const std::string &payload) const;
	/** Read a bounded line, retaining following bytes in pending; false on EOF/error. */
	bool line(std::string &pending, std::string &result) const;
};

/** Fixed set of joinable task threads. Shutdown drains tasks before joining. */
class Tasks {
	std::mutex mutex;
	std::condition_variable changed;
	std::deque<std::function<void()>> queue;
	bool closing = false;
	std::vector<std::thread> threads;
	/** Run queued operations; catches/logs failures and continues serving. */
	void run();
public:
	/** Start count threads; count must be positive. */
	explicit Tasks(unsigned count);
	/** Drain queued work and join all task threads. */
	~Tasks();
	/** Queue owned task, throwing if shutdown has begun. */
	void submit(std::function<void()> task);
	/** Finish queued tasks and join threads; repeated calls are allowed. */
	void close();
};

/** Worker pool; every member is protected by Manager::mutex. */
struct Pool {
	std::string name, executable; /**< Owned registration name and executable path. */
	std::deque<Worker> ready; /**< Live candidates awaiting allocation. */
	std::vector<Worker> children; /**< Idle and starting workers; allocated workers transfer out. */
	unsigned spawning = 0, target = 3; /**< Counted startups and steady-state target. */
	bool closing = false; /**< Prevent publication after replacement/shutdown. */
	std::string error; /**< Most recent startup failure, empty until one occurs. */
};

/** Shared pool policy and ownership; transport servers borrow it until they join. */
class Manager {
	std::string directory, pipe_directory;
	std::mutex mutex;
	std::condition_variable changed;
	std::map<std::string, std::shared_ptr<Pool>> pools;
	std::map<std::uint32_t, Worker> allocated;
	std::vector<Worker> retired; /**< Finished calls retained until the child can be reaped. */
	Tasks spawner{4};
	bool closing = false;
	bool closed = false;
	/** Run counted startup; always notify waiters on completion. */
	void spawn(const std::shared_ptr<Pool> &pool, unsigned timeout_ms);
	/** Queue startup under mutex; timeout_ms applies once execution begins. */
	void schedule(const std::shared_ptr<Pool> &pool, unsigned timeout_ms = 10000);
	/** Kill/reap this pool's children and wait for all counted startups. */
	void close_pool(const std::shared_ptr<Pool> &pool);
public:
	/** Own existing session-directory and optional Windows pipe-directory strings. */
	explicit Manager(std::string directory, std::string pipe_directory) :
		directory(std::move(directory)), pipe_directory(std::move(pipe_directory)) {}
	~Manager();
	/** Register/replace pool; return OK or ERROR text without newline. */
	std::string preload(const std::string &name, const std::string &executable);
	/** Allocate owned worker; caller supplies empty error, receiving ERROR text on failure.
	 * With wait=false an empty error and null worker mean not yet READY.
	 */
	Worker allocate(const std::string &name, std::string &error, bool wait = true);
	/** Queue replacement after delivery of the allocation reply. */
	void replenish(const std::string &name);
	/** End tracked call; optionally wait up to 10 s and return native status text. */
	std::string finish(const std::string &pid, bool report = false);
	/** Reap completed calls without blocking; invoked by owner-monitor loop. */
	void reap();
	/** Begin shutdown and wake requests; server joins its users before close(). */
	void stop();
	/** Wake waiters, kill idle/starting workers, drain spawner. Idempotent. */
	void close();
};

/** Platform request endpoint; destruction joins all requests before Manager destruction. */
class Server {
public:
	virtual ~Server() = default;
	/** Owner-monitor tick; return false after STOP or unrecoverable endpoint failure. */
	virtual bool tick() = 0;
};
/** Create platform server; all references remain borrowed until destruction. */
std::unique_ptr<Server> serve(Manager &manager, const std::string &directory,
			    const std::string &pipe_directory);
}
#endif
