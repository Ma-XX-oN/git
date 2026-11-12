#include "manager/manager.hpp"
#include <fstream>
#include <iostream>
#include <stdexcept>
#ifndef _WIN32
#include <signal.h>
#endif

namespace preload {
/** Escape borrowed UTF-8 as a JSON string, preserving printable Unicode bytes. */
static std::string json_string(const std::string &text)
{
	std::string result = "\"";
	for (unsigned char c : text) {
		if (c == '"' || c == '\\') { result += '\\'; result += c; }
		else if (c < 32) {
			const char hex[] = "0123456789abcdef";
			result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15];
		} else result += c;
	}
	return result + '"';
}

/** Remove only owned session endpoint names; retain directory and manager.log. */
static void cleanup(const std::filesystem::path &directory)
{
	std::error_code error;
	for (auto &entry : std::filesystem::directory_iterator(directory)) {
		auto name = entry.path().filename().string();
		if (name.rfind("worker-", 0) == 0 && std::filesystem::is_directory(entry.symlink_status())) {
			/* Worker directories contain endpoints, never nested user trees. */
			for (auto &channel : std::filesystem::directory_iterator(entry.path()))
				std::filesystem::remove(channel.path(), error);
			std::filesystem::remove(entry.path(), error);
		} else if (name == "request" || name.rfind("slot.", 0) == 0 || name == "ready.json" || name == "ready.tmp")
			std::filesystem::remove(entry.path(), error);
	}
}

int manager_main(int argc, char **argv)
{
	std::ios::sync_with_stdio(false);
	std::string directory, pipe_directory, owner_text;
	try {
		for (int i = 1; i < argc; ++i) {
			std::string option = argv[i];
			if (i + 1 == argc) throw std::runtime_error("missing option value");
			if (option == "--owner-pid") owner_text = argv[++i];
			else if (option == "--directory") directory = argv[++i];
			else if (option == "--pipe-dir") pipe_directory = argv[++i];
			else throw std::runtime_error("unknown option: " + option);
		}
		std::uint32_t owner_pid;
		if (!parse_pid(owner_text, owner_pid) || directory.empty()) throw std::runtime_error("required: --owner-pid PID --directory PATH");
#ifdef _WIN32
		if (pipe_directory.empty()) throw std::runtime_error("required: --pipe-dir NAME");
#else
		signal(SIGPIPE, SIG_IGN);
#endif
		auto path = std::filesystem::absolute(std::filesystem::u8path(directory));
		if (!std::filesystem::is_directory(path)) throw std::runtime_error("session directory does not exist");
		directory = path.u8string();
		Owner owner(owner_pid);
		set_environment("PRELOAD_PID", owner_text);
		if (!pipe_directory.empty()) set_environment("PRELOAD_WIN_DIR", pipe_directory);
		Manager manager(directory, pipe_directory);
		std::unique_ptr<Server> server;
		try {
			server = serve(manager, directory, pipe_directory);
			std::ofstream ready(path / "ready.tmp", std::ios::binary);
			ready << "{\"state\":\"READY\",\"manager_pid\":" << process_id() << ",\"owner_pid\":" << owner_pid;
			if (pipe_directory.empty()) ready << ",\"slots\":32";
			else ready << ",\"pipe_dir\":" << json_string(pipe_directory);
			ready << "}\n";
			ready.close();
			if (!ready) throw std::runtime_error("write readiness failed");
			std::filesystem::rename(path / "ready.tmp", path / "ready.json");
			log("READY " + (pipe_directory.empty() ? directory + "/request" : pipe_directory + "\\request"));
			while (owner.alive() && server->tick()) manager.reap();
			if (!owner.alive()) log("OWNER_EXIT " + owner_text);
		} catch (...) {
			manager.stop(); server.reset(); manager.close(); cleanup(path); throw;
		}
		std::filesystem::remove(path / "ready.json");
		manager.stop();
		server.reset();
		manager.close();
		cleanup(path);
		return 0;
	} catch (const std::exception &error) {
		std::cerr << "preload-manager: " << error.what() << std::endl;
		return 1;
	}
}
}
