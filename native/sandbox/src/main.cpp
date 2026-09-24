#include "sandbox.hpp"

#include <unistd.h>

#include <cerrno>
#include <charconv>
#include <algorithm>
#include <exception>
#include <filesystem>
#include <iostream>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <set>
#include <system_error>
#include <vector>

namespace neu_box::sandbox {
namespace {

namespace fs = std::filesystem;

void print_usage(std::ostream& output, std::string_view executable) {
    output << "cgroup 版本: v2\n\n"
           << "用法: " << executable
           << " [--bpf-object PATH] [--device-major MAJOR]"
              " {load|create|join|bind-container|unbind-container"
              "|status|destroy|list|cleanup}"
              " [参数]\n\n"
           << "  -h, --help\n"
           << "  --bpf-object PATH\n"
           << "  --device-major MAJOR  Python 发现的受管设备 major\n"
           << "  load\n"
           << "  create <name> <cpu> <mem> [major:minor ...]\n"
           << "          cpu    = CPU 核数 (0=不限)\n"
           << "          mem    = 内存 (512M / 2G / 0=不限)\n"
           << "          device = 设备号 (major:minor)\n"
           << "  join <name> <PID>\n"
           << "  bind-container <name> <mnt-ns>\n"
           << "         把一个容器的 mount namespace 登记到该 sandbox 的授权上\n"
           << "         （inum 由调用方 open+fstat 得到，它同时持有那个 fd 当 pin）\n"
           << "  unbind-container <mnt-ns>\n"
           << "         删除一条容器登记（容器退出时；destroy 也会一并清理）\n"
           << "  status <name>\n"
           << "  destroy <name>\n"
           << "  list\n"
           << "  cleanup\n";
}

auto parse_unsigned(std::string_view text, std::string_view description) -> std::uint64_t {
    if (text.empty()) {
        throw std::invalid_argument(std::string(description) + "不能为空");
    }
    std::uint64_t value = 0;
    const char* first = text.data();
    const char* last = first + text.size();
    const auto result = std::from_chars(first, last, value, 10);
    if (result.ec != std::errc() || result.ptr != last) {
        throw std::invalid_argument("非法" + std::string(description) + ": " + std::string(text));
    }
    return value;
}

auto parse_memory(std::string_view text) -> std::uint64_t {
    if (text == "0") {
        return 0;
    }
    if (text.empty()) {
        throw std::invalid_argument("内存限制不能为空");
    }

    std::uint64_t multiplier = 1;
    const char suffix = text.back();
    if (suffix == 'K' || suffix == 'k') {
        multiplier = 1024;
        text.remove_suffix(1);
    } else if (suffix == 'M' || suffix == 'm') {
        multiplier = 1024ULL * 1024ULL;
        text.remove_suffix(1);
    } else if (suffix == 'G' || suffix == 'g') {
        multiplier = 1024ULL * 1024ULL * 1024ULL;
        text.remove_suffix(1);
    } else {
        throw std::invalid_argument("无法识别的内存单位（支持 K/M/G，0 表示不限）");
    }

    const std::uint64_t number = parse_unsigned(text, "内存限制");
    if (number > std::numeric_limits<std::uint64_t>::max() / multiplier) {
        throw std::invalid_argument("内存限制溢出");
    }
    return number * multiplier;
}

auto parse_device(std::string_view text) -> DeviceId {
    const std::size_t colon = text.find(':');
    if (colon == std::string_view::npos) {
        throw std::invalid_argument("非法设备号（需要 major:minor）: " + std::string(text));
    }
    const std::string_view major_text = text.substr(0, colon);
    const std::string_view minor_text = text.substr(colon + 1);

    const std::uint64_t major = parse_unsigned(major_text, "设备 major");
    if (major > UINT32_MAX) {
        throw std::invalid_argument("设备 major 超出 uint32 范围");
    }

    const std::uint64_t minor = parse_unsigned(minor_text, "设备 minor");
    if (minor > UINT32_MAX) {
        throw std::invalid_argument("设备 minor 超出 uint32 范围");
    }
    return {static_cast<std::uint32_t>(major), static_cast<std::uint32_t>(minor)};
}

auto executable_path() -> fs::path {
    std::vector<char> buffer(4096);
    const ssize_t length = ::readlink("/proc/self/exe", buffer.data(), buffer.size() - 1);
    if (length < 0) {
        throw std::system_error(errno, std::generic_category(), "读取 /proc/self/exe");
    }
    buffer[static_cast<std::size_t>(length)] = '\0';
    return fs::path(buffer.data());
}

auto find_bpf_object(const std::optional<fs::path>& explicit_path) -> fs::path {
    if (explicit_path.has_value()) {
        return fs::absolute(*explicit_path);
    }

    // 发布布局要求 BPF object 与 native CLI 同目录。开发时若需要其他
    // 对象必须显式传 --bpf-object，不能从可写工作目录隐式拾取。
    return executable_path().parent_path() / "device_block.o";
}

void require_privilege() {
    const fs::path root_processes = fs::path(kCgroupRoot) / "cgroup.procs";
    if (::geteuid() != 0 && ::access(root_processes.c_str(), W_OK) != 0) {
        throw std::runtime_error("需要 root 或等效 cgroup/BPF 权限");
    }
}

void destroy_one(std::string_view name) {
    validate_sandbox_name(name);
    const bool has_state = state_exists(name);
    const bool has_live_cgroup = cgroup_exists(name);

    std::optional<std::uint64_t> stored_owner;
    std::string state_error;
    if (has_state) {
        try {
            stored_owner = read_state_cgroup_id(name);
        } catch (const std::exception& error) {
            state_error = error.what();
        }
    }

    // The state file is the only durable link to a cgroup owner that may no
    // longer match the live directory inode.  Never overwrite a malformed
    // record: doing so could make old map entries impossible to identify and
    // let destroy report success while the device remains reserved.
    if (has_state && !stored_owner.has_value()) {
        throw std::runtime_error("sandbox 状态损坏，无法确认 BPF map 已清理: " + state_error);
    }

    if (!has_live_cgroup) {
        if (!stored_owner.has_value()) {
            return;
        }
        release_all_devices(*stored_owner);
        remove_state(name);
        return;
    }

    const std::uint64_t live_owner = cgroup_id(name);
    if (stored_owner.has_value() && *stored_owner != live_owner) {
        // 先清理已经不对应当前 cgroup 的先前 owner；失败时保留当前 cgroup
        // 和状态，避免把无法重试的条目遗留在 map 中。
        release_all_devices(*stored_owner);
    }

    // 在删除 cgroup 前持久化它的实际 ID。后续 map 清理即使失败，下一次
    // destroy 仍能在 cgroup 目录消失后按 owner 重试。
    write_state_cgroup_id(name, live_owner);
    destroy_cgroup(name);
    release_all_devices(live_owner);
    remove_state(name);
}

auto dispatch(const std::vector<std::string_view>& arguments,
              const std::optional<fs::path>& bpf_object,
              const std::optional<std::uint32_t>& device_major,
              std::string_view executable) -> int {
    if (arguments.empty()) {
        print_usage(std::cerr, executable);
        return 2;
    }

    const std::string_view command = arguments[0];
    if (command == "--help" || command == "-h") {
        if (arguments.size() != 1) {
            throw std::invalid_argument("help 不接受参数");
        }
        print_usage(std::cout, executable);
        return 0;
    }

    require_privilege();
    ProcessLock lock;
    const fs::path object_path = find_bpf_object(bpf_object);

    if (command == "list") {
        if (arguments.size() != 1) {
            throw std::invalid_argument("list 不接受参数");
        }
        validate_bpf_list_ready(object_path);
        std::set<std::string> names_set;
        for (const auto& name : cgroup_names())
            names_set.insert(name);
        for (const auto& name : state_names())
            names_set.insert(name);
        const std::vector<std::string> names(names_set.begin(), names_set.end());
        if (names.empty()) {
            std::cout << "(无)\n";
        } else {
            for (const std::string& name : names) {
                std::cout << name << '\n';
            }
        }
        return 0;
    }

    if (command == "status") {
        if (arguments.size() != 2) {
            throw std::invalid_argument("用法: status <name>");
        }
        validate_bpf_status_ready(object_path);
        show_cgroup_status(arguments[1], std::cout);
        std::cout << "--- 设备预留 ---\n";
        dump_bpf_maps(std::cout);
        std::cout << "\n--- 进程列表 ---\n";
        show_cgroup_processes(arguments[1], std::cout);
        return 0;
    }

    if (command == "load") {
        if (arguments.size() != 1) {
            throw std::invalid_argument("load 不接受参数");
        }
        // load 是幂等的 ensure，不隐式删除活跃 sandbox 的 map。
        if (!device_major.has_value()) {
            throw std::invalid_argument("load 需要 --device-major");
        }
        ensure_bpf_ready(object_path, *device_major);
        std::cout << "✓ BPF 程序已加载并挂载\n";
        return 0;
    }

    if (command == "create") {
        if (arguments.size() < 4) {
            throw std::invalid_argument("用法: create <name> <cpu> <mem> [major:minor ...]");
        }
        const std::string_view name = arguments[1];
        validate_sandbox_name(name);
        const std::uint64_t cpu = parse_unsigned(arguments[2], "CPU 数量");
        const std::uint64_t memory = parse_memory(arguments[3]);
        std::vector<DeviceId> devices;
        for (std::size_t index = 4; index < arguments.size(); ++index) {
            devices.push_back(parse_device(arguments[index]));
        }

        if (state_exists(name)) {
            throw std::runtime_error("sandbox 存在待清理状态，请先执行 destroy: " +
                                     std::string(name));
        }

        if (!device_major.has_value()) {
            throw std::invalid_argument("create 需要 --device-major");
        }
        ensure_bpf_ready(object_path, *device_major);
        const std::uint64_t owner = create_cgroup(name, cpu, memory);
        try {
            // 先持久化恢复信息。即使进程在 BPF 更新期间被强制终止，
            // destroy/reaper 仍可按 cgroup ID 扫描并清理 map。
            write_state_cgroup_id(name, owner);
            reserve_devices(owner, devices);
        } catch (...) {
            const std::exception_ptr create_error = std::current_exception();
            try {
                // destroy_one 只有在 cgroup 与 map 都确认清理后才删除状态。
                destroy_one(name);
            } catch (const std::exception& rollback_error) {
                std::string original = "未知错误";
                try {
                    std::rethrow_exception(create_error);
                } catch (const std::exception& error) {
                    original = error.what();
                }
                throw std::runtime_error("create 失败且回滚不完整；已保留状态供 destroy 重试。"
                                         " 原因: " +
                                         original + "; 回滚: " + rollback_error.what());
            }
            std::rethrow_exception(create_error);
        }

        std::cout << "✓ sandbox '" << name << "' 已创建\n";
        return 0;
    }

    if (command == "join") {
        if (arguments.size() != 3) {
            throw std::invalid_argument("用法: join <name> <PID>");
        }
        const std::uint64_t parsed_pid = parse_unsigned(arguments[2], "PID");
        if (parsed_pid > static_cast<std::uint64_t>(std::numeric_limits<pid_t>::max())) {
            throw std::invalid_argument("PID 超出范围");
        }
        require_bpf_ready();
        join_cgroup(arguments[1], static_cast<pid_t>(parsed_pid));
        std::cout << "✓ PID " << parsed_pid << " 已加入 sandbox '" << arguments[1] << "'\n";
        return 0;
    }

    if (command == "bind-container") {
        if (arguments.size() != 3) {
            throw std::invalid_argument("用法: bind-container <name> <mnt-ns>");
        }
        const std::uint64_t mount_namespace = parse_unsigned(arguments[2], "mount namespace");
        if (mount_namespace == 0) {
            throw std::invalid_argument("mount namespace 不能为 0");
        }
        validate_sandbox_name(arguments[1]);
        require_bpf_ready();
        if (!cgroup_exists(arguments[1])) {
            throw std::runtime_error("sandbox 不存在: " + std::string(arguments[1]));
        }

        // 容器一定有自己的 mount namespace（docker 没有关掉它的开关）。
        // 和宿主机相同说明调用方给的不是容器，登记它等于把整个宿主机变成
        // 受托方，必须拒绝。
        if (mount_namespace == mount_namespace_of(::getpid())) {
            throw std::runtime_error("mnt ns " + std::to_string(mount_namespace) +
                                     " 是宿主机自己的 mount namespace，不是容器");
        }

        const std::uint64_t owner = cgroup_id(arguments[1]);
        register_container_owner(mount_namespace, owner);
        std::cout << "✓ 容器 mnt ns " << mount_namespace << " 已登记到 sandbox '" << arguments[1]
                  << "'\n";
        return 0;
    }

    if (command == "unbind-container") {
        if (arguments.size() != 2) {
            throw std::invalid_argument("用法: unbind-container <mnt-ns>");
        }
        const std::uint64_t mount_namespace = parse_unsigned(arguments[1], "mount namespace");
        if (mount_namespace == 0) {
            throw std::invalid_argument("mount namespace 不能为 0");
        }
        // 用 mnt ns 而不是 PID: 容器退出后 /proc/<pid> 已经消失，只能按
        // 登记时记下的 inum 删除。
        require_bpf_ready();
        unregister_container_owner(mount_namespace);
        std::cout << "✓ mnt ns " << mount_namespace << " 的容器登记已删除\n";
        return 0;
    }

    if (command == "destroy") {
        if (arguments.size() != 2) {
            throw std::invalid_argument("用法: destroy <name>");
        }
        destroy_one(arguments[1]);
        std::cout << "✓ sandbox '" << arguments[1] << "' 已销毁\n";
        return 0;
    }

    if (command == "cleanup") {
        if (arguments.size() != 1) {
            throw std::invalid_argument("cleanup 不接受参数");
        }
        // State files can outlive a cgroup when a previous destroy was
        // interrupted after removing the directory. Include them so
        // release_all_devices() runs before pins/state are removed.
        std::set<std::string> names_set;
        for (const auto& name : cgroup_names())
            names_set.insert(name);
        for (const auto& name : state_names())
            names_set.insert(name);
        const std::vector<std::string> names(names_set.begin(), names_set.end());
        std::size_t failures = 0;
        for (const std::string& name : names) {
            try {
                destroy_one(name);
            } catch (const std::exception& error) {
                ++failures;
                std::cerr << "清理 sandbox '" << name << "' 失败: " << error.what() << '\n';
            }
        }
        const std::vector<std::string> remaining_cgroups = cgroup_names();
        const std::vector<std::string> remaining_state = state_names();
        if (failures != 0 || !remaining_cgroups.empty() || !remaining_state.empty()) {
            throw std::runtime_error("cleanup 有 " + std::to_string(failures) +
                                     " 项失败或仍有残留 cgroup；保留 BPF 隔离");
        }

        unload_bpf();
        remove_all_state();
        std::cout << "✓ cleanup 完成\n";
        return 0;
    }

    throw std::invalid_argument("未知命令: " + std::string(command));
}

} // namespace
} // namespace neu_box::sandbox

auto main(int argc, char* argv[]) -> int {
    using namespace neu_box::sandbox;

    try {
        std::optional<std::filesystem::path> bpf_object;
        std::optional<std::uint32_t> device_major;
        int index = 1;
        while (index < argc) {
            const std::string_view option(argv[index]);
            if (option == "--bpf-object") {
                if (index + 1 >= argc) {
                    throw std::invalid_argument("--bpf-object 缺少路径");
                }
                bpf_object = argv[index + 1];
                index += 2;
                continue;
            }
            if (option == "--device-major") {
                if (index + 1 >= argc) {
                    throw std::invalid_argument("--device-major 缺少值");
                }
                const std::uint64_t value = parse_unsigned(argv[index + 1], "设备 major");
                if (value > UINT32_MAX) {
                    throw std::invalid_argument("设备 major 超出 uint32 范围");
                }
                device_major = static_cast<std::uint32_t>(value);
                index += 2;
                continue;
            }
            break;
        }

        std::vector<std::string_view> arguments;
        for (; index < argc; ++index) {
            arguments.emplace_back(argv[index]);
        }
        return dispatch(arguments, bpf_object, device_major, argv[0]);
    } catch (const std::exception& error) {
        std::cerr << "错误: " << error.what() << '\n';
        return 1;
    }
}
