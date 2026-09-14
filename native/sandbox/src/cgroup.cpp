#include "sandbox.hpp"

#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cerrno>
#include <fstream>
#include <iterator>
#include <limits>
#include <ostream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

namespace neu_box::sandbox {
namespace {

namespace fs = std::filesystem;

[[noreturn]]
void throw_errno(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

auto path_for(std::string_view name) -> fs::path {
    validate_sandbox_name(name);
    return fs::path(kCgroupRoot) / (std::string(kCgroupPrefix) + std::string(name));
}

void write_file(const fs::path& path, std::string_view value) {
    std::ofstream output(path);
    output << value;
    output.close();
    if (!output) {
        throw std::runtime_error("无法写入 " + path.string());
    }
}

auto try_write(const fs::path& path, std::string_view value) -> bool {
    try {
        write_file(path, value);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

auto read_line(const fs::path& path) -> std::string {
    std::ifstream input(path);
    std::string value;
    if (!std::getline(input, value)) {
        throw std::runtime_error("无法读取 " + path.string());
    }
    return value;
}

auto event_is(const fs::path& cgroup, std::string_view expected, int expected_value) -> bool {
    std::ifstream input(cgroup / "cgroup.events");
    std::string name;
    int value = 0;
    while (input >> name >> value) {
        if (name == expected) {
            return value == expected_value;
        }
    }
    return false;
}

auto collect_processes(const fs::path& cgroup) -> std::vector<pid_t> {
    std::vector<pid_t> processes;
    for (const fs::directory_entry& entry :
         fs::recursive_directory_iterator(cgroup, fs::directory_options::skip_permission_denied)) {
        if (entry.path().filename() != "cgroup.procs") {
            continue;
        }
        std::ifstream input(entry.path());
        pid_t process = 0;
        while (input >> process) {
            processes.push_back(process);
        }
    }
    return processes;
}

void wait_until_empty(const fs::path& cgroup, int attempts) {
    for (int attempt = 0; attempt < attempts; ++attempt) {
        if (!fs::exists(cgroup) || event_is(cgroup, "populated", 0)) {
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

void kill_processes(const fs::path& cgroup) {
    if (try_write(cgroup / "cgroup.freeze", "1")) {
        for (int attempt = 0; attempt < 20; ++attempt) {
            if (event_is(cgroup, "frozen", 1)) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    if (try_write(cgroup / "cgroup.kill", "1")) {
        wait_until_empty(cgroup, 50);
    }
    for (pid_t process : collect_processes(cgroup)) {
        ::kill(process, SIGKILL);
    }
    wait_until_empty(cgroup, 20);
}

void remove_children(const fs::path& cgroup) {
    std::vector<fs::path> children;
    for (const fs::directory_entry& entry :
         fs::recursive_directory_iterator(cgroup, fs::directory_options::skip_permission_denied)) {
        if (entry.is_directory()) {
            children.push_back(entry.path());
        }
    }
    for (auto child = children.rbegin(); child != children.rend(); ++child) {
        std::error_code ignored;
        fs::remove(*child, ignored);
    }
}

auto command_line(pid_t process) -> std::string {
    std::ifstream input("/proc/" + std::to_string(process) + "/cmdline", std::ios::binary);
    if (!input) {
        return "(已退出)";
    }
    std::string command{std::istreambuf_iterator<char>{input}, {}};
    std::replace(command.begin(), command.end(), '\0', ' ');
    return command;
}

} // namespace

void validate_sandbox_name(std::string_view name) {
    if (name.empty() || name.size() > 128 || name == "." || name == "..") {
        throw std::invalid_argument("非法 sandbox 名称");
    }
    for (char character : name) {
        const unsigned char value = static_cast<unsigned char>(character);
        if (!std::isalnum(value) && character != '_' && character != '-' && character != '.') {
            throw std::invalid_argument("sandbox 名称只能包含字母、数字、_、-、.");
        }
    }
}

auto cgroup_exists(std::string_view name) -> bool {
    return fs::is_directory(path_for(name));
}

auto cgroup_id(std::string_view name) -> std::uint64_t {
    const fs::path path = path_for(name);
    struct stat status {};
    if (::stat(path.c_str(), &status) != 0) {
        throw_errno("读取 cgroup ID: " + path.string());
    }
    return static_cast<std::uint64_t>(status.st_ino);
}

auto mount_namespace_of(pid_t process) -> std::uint64_t {
    if (process <= 0) {
        throw std::invalid_argument("PID 非法: " + std::to_string(process));
    }
    const fs::path path = "/proc/" + std::to_string(process) + "/ns/mnt";
    struct stat status {};
    if (::stat(path.c_str(), &status) != 0) {
        if (errno == ENOENT) {
            throw std::runtime_error("进程不存在，无法读取 mount namespace: PID " +
                                     std::to_string(process));
        }
        throw_errno("读取 mount namespace: " + path.string());
    }
    return static_cast<std::uint64_t>(status.st_ino);
}

auto create_cgroup(std::string_view name, std::uint64_t cpu_count,
                   std::uint64_t memory_bytes) -> std::uint64_t {
    const fs::path path = path_for(name);
    if (fs::exists(path)) {
        throw std::runtime_error("sandbox 已存在: " + std::string(name));
    }

    try_write(fs::path(kCgroupRoot) / "cgroup.subtree_control", "+cpu +memory");
    fs::create_directories(path);
    try {
        if (cpu_count != 0) {
            if (cpu_count > std::numeric_limits<std::uint64_t>::max() / 100000) {
                throw std::invalid_argument("CPU 数量过大");
            }
            write_file(path / "cpu.max", std::to_string(cpu_count * 100000) + " 100000");
        }
        if (memory_bytes != 0) {
            write_file(path / "memory.max", std::to_string(memory_bytes));
            if (fs::exists(path / "memory.swap.max")) {
                write_file(path / "memory.swap.max", "0");
            }
        }
        return cgroup_id(name);
    } catch (...) {
        std::error_code ignored;
        fs::remove(path, ignored);
        throw;
    }
}

void join_cgroup(std::string_view name, pid_t process) {
    if (process <= 0 || !fs::is_directory("/proc/" + std::to_string(process))) {
        throw std::invalid_argument("PID 不存在: " + std::to_string(process));
    }
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        throw std::runtime_error("sandbox 不存在: " + std::string(name));
    }
    write_file(path / "cgroup.procs", std::to_string(process));
}

void destroy_cgroup(std::string_view name) {
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        return;
    }
    kill_processes(path);
    remove_children(path);
    // 仍有进程时删除会失败，调用方不会继续释放设备预留。
    fs::remove(path);
}

auto cgroup_names() -> std::vector<std::string> {
    std::vector<std::string> names;
    const std::string prefix(kCgroupPrefix);
    for (const fs::directory_entry& entry : fs::directory_iterator(kCgroupRoot)) {
        const std::string filename = entry.path().filename().string();
        if (entry.is_directory() && filename.rfind(prefix, 0) == 0) {
            names.push_back(filename.substr(prefix.size()));
        }
    }
    std::sort(names.begin(), names.end());
    return names;
}

void show_cgroup_status(std::string_view name, std::ostream& output) {
    const fs::path path = path_for(name);
    if (!fs::is_directory(path)) {
        throw std::runtime_error("sandbox 不存在: " + std::string(name));
    }

    const std::string cpu = read_line(path / "cpu.max");
    const std::string limit = read_line(path / "memory.max");
    const std::string usage = read_line(path / "memory.current");
    output << "=== 沙盒: " << name << " ===\n"
           << "cgroup: v2\n\n--- CPU ---\n"
           << cpu << "\n\n--- 内存 ---\n";
    if (limit == "max") {
        output << "  limit:  不限\n";
    } else {
        output << "  limit:  " << limit << " bytes (" << (std::stoull(limit) / 1024 / 1024)
               << "M)\n";
    }
    output << "  usage:  " << usage << " bytes\n\n";
}

void show_cgroup_processes(std::string_view name, std::ostream& output) {
    const fs::path path = path_for(name);
    std::ifstream processes(path / "cgroup.procs");
    if (!processes) {
        throw std::runtime_error("无法读取 " + (path / "cgroup.procs").string());
    }
    pid_t process = 0;
    bool empty = true;
    while (processes >> process) {
        output << "  PID " << process << "  " << command_line(process) << '\n';
        empty = false;
    }
    if (empty) {
        output << "  (空)\n";
    }
}

} // namespace neu_box::sandbox
