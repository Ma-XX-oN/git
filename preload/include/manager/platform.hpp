#ifndef PRELOAD_MANAGER_PLATFORM_HPP
#define PRELOAD_MANAGER_PLATFORM_HPP

#include <cstdint>
#include <memory>
#include <string>
#include "shared/connection.h"

namespace preload {
/** Owned child process; status calls are synchronized and cache the native result. */
class Process {
public:
	struct Impl; /**< Platform-owned process and READY pipe state. */
	std::unique_ptr<Impl> impl;
	std::uint32_t pid = 0; /**< Native PID, nonzero after successful spawn. */
	preload_connection context = -1; /**< Owned optional preconnected Windows context; transfers to GET. */
	/** Spawn executable --preload-worker directory with inherited startup environment.
	 * Throws on failure. All strings are borrowed for the duration of this call.
	 */
	Process(const std::string &executable, const std::string &directory);
	/** Close process/READY resources; caller must reap or retain running children. */
	~Process();
	/** Read and validate READY within timeout_ms; close READY pipe; false on failure. */
	bool ready(unsigned timeout_ms);
	/** Query cached/native status; false while running; code is EXIT or signal number. */
	bool status(std::uint32_t &code, bool &signal);
	/** Terminate a running child; already exited is permitted. Does not reap. */
	void kill();
};

/** Owner reference pins native identity on Windows. POSIX uses its native PID. */
class Owner {
	struct Impl;
	std::unique_ptr<Impl> impl;
public:
	/** Validate positive native PID and acquire monitoring resources; throws on failure. */
	explicit Owner(std::uint32_t pid);
	~Owner();
	/** Return whether owner still exists; permission denial counts as alive on POSIX. */
	bool alive() const;
};
/** Return native current process ID. */
std::uint32_t process_id();
/** Set startup environment variable before any threads/children exist; throws on failure. */
void set_environment(const std::string &name, const std::string &value);
/** Native UTF-8 process arguments; used by Windows wmain and POSIX main. */
int manager_main(int argc, char **argv);
}
#endif
