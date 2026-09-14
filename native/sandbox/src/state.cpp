#include "sandbox.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <cerrno>
#include <algorithm>
#include <fstream>
#include <stdexcept>
#include <string>
#include <system_error>

namespace neu_box::sandbox {
namespace {

namespace fs = std::filesystem;
inline constexpr std::string_view kStateDirectory = "/run/neu-box/sandbox-state";

[[noreturn]]
void throw_errno(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

auto cgroup_state_path(std::string_view name) -> fs::path {
    return fs::path(kStateDirectory) / ("cgroup_id_" + std::string(name));
}

void write_atomic(const fs::path& target, const std::string& content) {
    // 临时文件名带前导点：不能命中 state_names() 的 "cgroup_id_" 前缀，
    // 否则进程在 create 与 rename 之间被杀时，残留的临时文件会被当成一个
    // sandbox 名字（validate_sandbox_name 允许点号），让 residuals 永不为空。
    const fs::path temporary = target.parent_path() / ("." + target.filename().string() + ".tmp." +
                                                       std::to_string(::getpid()));
    const int descriptor =
        ::open(temporary.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (descriptor < 0) {
        throw_errno("创建状态临时文件 " + temporary.string());
    }

    const char* cursor = content.data();
    std::size_t remaining = content.size();
    try {
        while (remaining > 0) {
            const ssize_t written = ::write(descriptor, cursor, remaining);
            if (written < 0) {
                throw_errno("写入状态临时文件 " + temporary.string());
            }
            if (written == 0) {
                throw std::runtime_error("写入状态文件返回 0 字节: " + temporary.string());
            }
            cursor += written;
            remaining -= static_cast<std::size_t>(written);
        }
        if (::fsync(descriptor) != 0) {
            throw_errno("同步状态临时文件 " + temporary.string());
        }
    } catch (...) {
        ::close(descriptor);
        std::error_code ignored;
        fs::remove(temporary, ignored);
        throw;
    }
    if (::close(descriptor) != 0) {
        const int saved_errno = errno;
        std::error_code ignored;
        fs::remove(temporary, ignored);
        errno = saved_errno;
        throw_errno("关闭状态临时文件 " + temporary.string());
    }
    fs::rename(temporary, target);
}

} // namespace

ProcessLock::ProcessLock() {
    fs::create_directories(fs::path(kLockFile).parent_path());
    const std::string path(kLockFile);
    descriptor_ = ::open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (descriptor_ < 0) {
        throw_errno("打开 sandbox 锁文件");
    }
    if (::flock(descriptor_, LOCK_EX) != 0) {
        const int saved_errno = errno;
        ::close(descriptor_);
        errno = saved_errno;
        throw_errno("锁定 sandbox 状态");
    }
}

ProcessLock::~ProcessLock() {
    ::close(descriptor_); // close 同时释放 flock
}

void write_state_cgroup_id(std::string_view name, std::uint64_t cgroup_id) {
    validate_sandbox_name(name);
    if (cgroup_id == 0) {
        throw std::invalid_argument("cgroup ID 不能为 0");
    }
    fs::create_directories(kStateDirectory);
    write_atomic(cgroup_state_path(name), std::to_string(cgroup_id) + '\n');
}

auto read_state_cgroup_id(std::string_view name) -> std::uint64_t {
    validate_sandbox_name(name);
    std::uint64_t cgroup_id = 0;
    std::ifstream cgroup_input(cgroup_state_path(name));
    if (!(cgroup_input >> cgroup_id) || cgroup_id == 0) {
        throw std::runtime_error("缺少有效 cgroup ID 状态: " + std::string(name));
    }
    return cgroup_id;
}

auto state_exists(std::string_view name) -> bool {
    validate_sandbox_name(name);
    return fs::exists(fs::symlink_status(cgroup_state_path(name)));
}

void remove_state(std::string_view name) {
    validate_sandbox_name(name);
    std::error_code error;
    fs::remove(cgroup_state_path(name), error);
    if (error) {
        throw std::system_error(error, "删除 sandbox cgroup 状态");
    }
}

void remove_all_state() {
    std::error_code error;
    fs::directory_iterator iterator(kStateDirectory, fs::directory_options::skip_permission_denied,
                                    error);
    if (error == std::errc::no_such_file_or_directory) {
        return;
    }
    if (error) {
        throw std::system_error(error, "读取 sandbox 状态目录");
    }

    const fs::directory_iterator end;
    while (iterator != end) {
        const fs::directory_entry& entry = *iterator;
        const std::string filename = entry.path().filename().string();
        if (filename.compare(0, 10, "cgroup_id_") == 0) {
            fs::remove(entry.path(), error);
            if (error) {
                throw std::system_error(error, "删除 sandbox 状态");
            }
        }
        iterator.increment(error);
        if (error) {
            throw std::system_error(error, "遍历 sandbox 状态目录");
        }
    }

    fs::remove(kStateDirectory, error);
    if (error) {
        throw std::system_error(error, "删除 sandbox 状态目录");
    }
}

auto state_names() -> std::vector<std::string> {
    std::vector<std::string> names;
    std::error_code error;
    fs::directory_iterator iterator(kStateDirectory, fs::directory_options::skip_permission_denied,
                                    error);
    if (error == std::errc::no_such_file_or_directory) {
        return names;
    }
    if (error) {
        throw std::system_error(error, "读取 sandbox 状态目录");
    }
    const fs::directory_iterator end;
    constexpr std::string_view prefix = "cgroup_id_";
    for (; iterator != end; iterator.increment(error)) {
        if (error) {
            throw std::system_error(error, "遍历 sandbox 状态目录");
        }
        const auto& entry = *iterator;
        const std::string filename = entry.path().filename().string();
        if (filename.compare(0, prefix.size(), prefix) != 0 || filename.size() == prefix.size()) {
            continue;
        }
        const std::string name = filename.substr(prefix.size());
        try {
            validate_sandbox_name(name);
        } catch (const std::exception&) {
            // Ignore unrelated files in the state directory; cleanup must
            // never turn an operator's diagnostic file into a sandbox name.
            continue;
        }
        names.push_back(name);
    }
    std::sort(names.begin(), names.end());
    return names;
}

} // namespace neu_box::sandbox
