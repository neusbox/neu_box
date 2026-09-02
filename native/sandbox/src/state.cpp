#include "sandbox.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <cerrno>
#include <fstream>
#include <stdexcept>
#include <string>
#include <system_error>

namespace neu_box::sandbox {
namespace {

namespace fs = std::filesystem;
inline constexpr std::string_view kStateDirectory =
    "/run/neu-box/sandbox-state";

[[noreturn]] void throw_errno(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

fs::path cgroup_state_path(std::string_view name) {
    return fs::path(kStateDirectory) /
           ("cgroup_id_" + std::string(name));
}

void write_atomic(const fs::path& target, const std::string& content) {
    const fs::path temporary =
        target.string() + ".tmp." + std::to_string(::getpid());
    const int descriptor = ::open(temporary.c_str(),
                                  O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC |
                                      O_NOFOLLOW,
                                  0600);
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
                throw std::runtime_error("写入状态文件返回 0 字节: " +
                                         temporary.string());
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

}  // namespace

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
        descriptor_ = -1;
        errno = saved_errno;
        throw_errno("锁定 sandbox 状态");
    }
}

ProcessLock::~ProcessLock() {
    release();
}

void ProcessLock::release() {
    if (descriptor_ >= 0) {
        ::flock(descriptor_, LOCK_UN);
        ::close(descriptor_);
        descriptor_ = -1;
    }
}

void write_state_cgroup_id(std::string_view name, std::uint64_t cgroup_id) {
    validate_sandbox_name(name);
    if (cgroup_id == 0) {
        throw std::invalid_argument("cgroup ID 不能为 0");
    }
    fs::create_directories(kStateDirectory);
    write_atomic(cgroup_state_path(name), std::to_string(cgroup_id) + '\n');
}

std::uint64_t read_state_cgroup_id(std::string_view name) {
    validate_sandbox_name(name);
    std::uint64_t cgroup_id = 0;
    std::ifstream cgroup_input(cgroup_state_path(name));
    if (!(cgroup_input >> cgroup_id) || cgroup_id == 0) {
        throw std::runtime_error("缺少有效 cgroup ID 状态: " +
                                 std::string(name));
    }
    return cgroup_id;
}

bool state_exists(std::string_view name) {
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
    fs::directory_iterator iterator(
        kStateDirectory, fs::directory_options::skip_permission_denied, error);
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

}  // namespace neu_box::sandbox
