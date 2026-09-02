#include "sandbox.hpp"

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cctype>
#include <exception>
#include <fstream>
#include <iterator>
#include <limits>
#include <ostream>
#include <set>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

namespace neu_box::sandbox {
namespace {

namespace fs = std::filesystem;

[[noreturn]] void throw_errno(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

fs::path path_for(std::string_view name) {
    return fs::path(kCgroupRoot) /
           (std::string(kCgroupPrefix) + std::string(name));
}

void write_text(const fs::path& path, std::string_view value) {
    const int descriptor = ::open(path.c_str(), O_WRONLY | O_CLOEXEC);
    if (descriptor < 0) {
        throw_errno("打开 " + path.string());
    }

    const char* cursor = value.data();
    std::size_t remaining = value.size();
    while (remaining > 0) {
        const ssize_t written = ::write(descriptor, cursor, remaining);
        if (written < 0) {
            const int saved_errno = errno;
            ::close(descriptor);
            errno = saved_errno;
            throw_errno("写入 " + path.string());
        }
        if (written == 0) {
            ::close(descriptor);
            throw std::runtime_error("写入返回 0 字节: " + path.string());
        }
        cursor += written;
        remaining -= static_cast<std::size_t>(written);
    }
    if (::close(descriptor) != 0) {
        throw_errno("关闭 " + path.string());
    }
}

void write_best_effort(const fs::path& path, std::string_view value) {
    try {
        write_text(path, value);
    } catch (const std::exception&) {
    }
}

std::string read_text(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw std::runtime_error("无法读取 " + path.string());
    }
    return std::string(std::istreambuf_iterator<char>(input),
                       std::istreambuf_iterator<char>());
}

std::string strip_line_endings(std::string value) {
    while (!value.empty() &&
           (value.back() == '\n' || value.back() == '\r')) {
        value.pop_back();
    }
    return value;
}

bool populated(const fs::path& path) {
    std::ifstream events(path / "cgroup.events");
    if (!events) {
        if (!fs::exists(path)) {
            return false;
        }
        throw std::runtime_error("无法读取 " +
                                 (path / "cgroup.events").string());
    }
    std::string key;
    int value = 0;
    while (events >> key >> value) {
        if (key == "populated") {
            return value != 0;
        }
    }
    throw std::runtime_error("cgroup.events 缺少 populated: " +
                             path.string());
}

std::vector<pid_t> collect_processes(const fs::path& path) {
    std::set<pid_t> processes;
    const auto collect_file = [&processes](const fs::path& file) {
        std::ifstream input(file);
        pid_t process = 0;
        while (input >> process) {
            if (process > 0) {
                processes.insert(process);
            }
        }
    };

    collect_file(path / "cgroup.procs");
    for (const fs::directory_entry& entry : fs::recursive_directory_iterator(
             path, fs::directory_options::skip_permission_denied)) {
        if (entry.path().filename() == "cgroup.procs") {
            collect_file(entry.path());
        }
    }
    return {processes.begin(), processes.end()};
}

void wait_until_empty(const fs::path& path, int attempts) {
    for (int attempt = 0; attempt < attempts; ++attempt) {
        if (!fs::exists(path) || !populated(path)) {
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

void kill_processes(const fs::path& path) {
    if (!fs::is_directory(path)) {
        return;
    }

    if (fs::exists(path / "cgroup.freeze")) {
        write_best_effort(path / "cgroup.freeze", "1");
        for (int attempt = 0; attempt < 20; ++attempt) {
            if (read_text(path / "cgroup.events").find("frozen 1") !=
                std::string::npos) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    if (fs::exists(path / "cgroup.kill")) {
        write_best_effort(path / "cgroup.kill", "1");
        wait_until_empty(path, 50);
    }

    for (pid_t process : collect_processes(path)) {
        ::kill(process, SIGKILL);
    }
    wait_until_empty(path, 20);

    const fs::path root_processes = fs::path(kCgroupRoot) / "cgroup.procs";
    for (pid_t process : collect_processes(path)) {
        write_best_effort(root_processes, std::to_string(process));
    }
}

void remove_children(const fs::path& path) {
    if (!fs::is_directory(path)) {
        return;
    }
    std::vector<fs::path> directories;
    for (const fs::directory_entry& entry : fs::recursive_directory_iterator(
             path, fs::directory_options::skip_permission_denied)) {
        if (entry.is_directory()) {
            directories.push_back(entry.path());
        }
    }
    std::sort(directories.begin(), directories.end(),
              [](const fs::path& left, const fs::path& right) {
                  return std::distance(left.begin(), left.end()) >
                         std::distance(right.begin(), right.end());
              });
    for (const fs::path& directory : directories) {
        std::error_code ignored;
        fs::remove(directory, ignored);
    }
}

std::string command_line(pid_t process) {
    std::ifstream input("/proc/" + std::to_string(process) + "/cmdline",
                        std::ios::binary);
    if (!input) {
        return "(已退出)";
    }
    std::string command{
        std::istreambuf_iterator<char>{input},
        std::istreambuf_iterator<char>{},
    };
    std::replace(command.begin(), command.end(), '\0', ' ');
    return command;
}

}  // namespace

void validate_sandbox_name(std::string_view name) {
    if (name.empty() || name.size() > 128 || name == "." || name == "..") {
        throw std::invalid_argument("非法 sandbox 名称");
    }
    for (char character : name) {
        const unsigned char value = static_cast<unsigned char>(character);
        if (!std::isalnum(value) && character != '_' && character != '-' &&
            character != '.') {
            throw std::invalid_argument(
                "sandbox 名称只能包含字母、数字、_、-、.");
        }
    }
}

bool cgroup_exists(std::string_view name) {
    validate_sandbox_name(name);
    return fs::is_directory(path_for(name));
}

std::uint64_t cgroup_id(std::string_view name) {
    validate_sandbox_name(name);
    const fs::path path = path_for(name);
    struct stat status {};
    if (::stat(path.c_str(), &status) != 0) {
        throw_errno("读取 cgroup ID: " + path.string());
    }
    return static_cast<std::uint64_t>(status.st_ino);
}

std::uint64_t create_cgroup(std::string_view name,
                            std::uint64_t cpu_count,
                            std::uint64_t memory_bytes) {
    validate_sandbox_name(name);
    const fs::path path = path_for(name);
    if (fs::exists(path)) {
        throw std::runtime_error("sandbox 已存在: " + std::string(name));
    }

    write_best_effort(fs::path(kCgroupRoot) / "cgroup.subtree_control",
                      "+cpu +memory");
    try {
        fs::create_directories(path);
        if (!fs::is_directory(path)) {
            throw std::runtime_error("无法创建 cgroup: " + path.string());
        }
        if (cpu_count != 0) {
            if (cpu_count >
                std::numeric_limits<std::uint64_t>::max() / 100000) {
                throw std::invalid_argument("CPU 数量过大");
            }
            write_text(path / "cpu.max",
                       std::to_string(cpu_count * 100000) + " 100000");
        }
        if (memory_bytes != 0) {
            write_text(path / "memory.max", std::to_string(memory_bytes));
            if (fs::exists(path / "memory.swap.max")) {
                write_text(path / "memory.swap.max", "0");
            }
        }
        return cgroup_id(name);
    } catch (...) {
        const std::exception_ptr create_error = std::current_exception();

        std::error_code remove_error;
        fs::remove(path, remove_error);
        std::error_code exists_error;
        const bool remains = fs::exists(path, exists_error);
        if (remove_error || exists_error || remains) {
            throw std::runtime_error(
                "创建 cgroup 失败，且回滚后目录仍然存在: " +
                path.string());
        }
        std::rethrow_exception(create_error);
    }
}

void join_cgroup(std::string_view name, pid_t process) {
    validate_sandbox_name(name);
    if (process <= 0 ||
        !fs::is_directory("/proc/" + std::to_string(process))) {
        throw std::invalid_argument("PID 不存在: " +
                                    std::to_string(process));
    }
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        throw std::runtime_error("sandbox 不存在: " + std::string(name));
    }
    write_text(path / "cgroup.procs", std::to_string(process));
}

void destroy_cgroup(std::string_view name) {
    validate_sandbox_name(name);
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        return;
    }

    kill_processes(path);
    remove_children(path);
    if (!fs::remove(path) && fs::exists(path)) {
        throw std::runtime_error("无法删除 cgroup: " + path.string());
    }
}

std::vector<std::string> cgroup_names() {
    std::vector<std::string> result;
    const std::string prefix(kCgroupPrefix);
    for (const fs::directory_entry& entry : fs::directory_iterator(
             fs::path(kCgroupRoot),
             fs::directory_options::skip_permission_denied)) {
        if (!entry.is_directory()) {
            continue;
        }
        const std::string filename = entry.path().filename().string();
        if (filename.size() >= prefix.size() &&
            filename.compare(0, prefix.size(), prefix) == 0) {
            result.push_back(filename.substr(prefix.size()));
        }
    }
    std::sort(result.begin(), result.end());
    return result;
}

void show_cgroup_status(std::string_view name, std::ostream& output) {
    validate_sandbox_name(name);
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        throw std::runtime_error("sandbox 不存在: " + std::string(name));
    }

    const std::string cpu = strip_line_endings(read_text(path / "cpu.max"));
    const std::string limit =
        strip_line_endings(read_text(path / "memory.max"));
    const std::string usage =
        strip_line_endings(read_text(path / "memory.current"));

    output << "=== 沙盒: " << name << " ===\n"
           << "cgroup: v2\n\n"
           << "--- CPU ---\n"
           << cpu << "\n\n"
           << "--- 内存 ---\n";

    if (limit == "9223372036854771712" || limit == "-1" ||
        limit == "max") {
        output << "  limit:  不限\n";
    } else {
        std::uint64_t bytes = 0;
        const auto parsed = std::from_chars(
            limit.data(), limit.data() + limit.size(), bytes, 10);
        if (parsed.ec != std::errc() ||
            parsed.ptr != limit.data() + limit.size()) {
            throw std::runtime_error("非法 memory.max 内容: " + limit);
        }
        output << "  limit:  " << limit << " bytes ("
               << (bytes / 1024 / 1024) << "M)\n";
    }
    output << "  usage:  " << usage << " bytes\n\n";
}

void show_cgroup_processes(std::string_view name, std::ostream& output) {
    validate_sandbox_name(name);
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        throw std::runtime_error("sandbox 不存在: " + std::string(name));
    }

    std::ifstream processes(path / "cgroup.procs");
    if (!processes) {
        throw std::runtime_error("无法读取 " +
                                 (path / "cgroup.procs").string());
    }
    pid_t process = 0;
    bool found = false;
    while (processes >> process) {
        output << "  PID " << process << "  " << command_line(process)
               << '\n';
        found = true;
    }
    if (!found) {
        output << "  (空)\n";
    }
}

}  // namespace neu_box::sandbox
