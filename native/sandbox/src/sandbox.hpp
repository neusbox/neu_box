#ifndef NEU_BOX_NATIVE_SANDBOX_HPP
#define NEU_BOX_NATIVE_SANDBOX_HPP

#include <cstdint>
#include <filesystem>
#include <iosfwd>
#include <optional>
#include <string>
#include <string_view>
#include <sys/types.h>
#include <utility>
#include <vector>

namespace neu_box::sandbox {

inline constexpr std::string_view kCgroupRoot = "/sys/fs/cgroup";
inline constexpr std::string_view kCgroupPrefix = "sandbox_";

inline constexpr std::string_view kBpfProgramPin =
    "/sys/fs/bpf/device_block";
inline constexpr std::string_view kBpfMapDirectory =
    "/sys/fs/bpf/sandbox_maps";
inline constexpr std::string_view kReservedDevicesPin =
    "/sys/fs/bpf/sandbox_maps/reserved_devices";
inline constexpr std::string_view kReservedMajorsPin =
    "/sys/fs/bpf/sandbox_maps/reserved_majors";
inline constexpr std::string_view kContainerOwnerPin =
    "/sys/fs/bpf/sandbox_maps/container_owner";

inline constexpr std::string_view kLockFile =
    "/run/neu-box/sandbox.lock";

struct DeviceId {
    std::uint32_t major;
    std::uint32_t minor;
};

struct CgroupMajorKey {
    std::uint64_t cgroup_id;
    std::uint32_t major;
    std::uint32_t padding;
};

// .rodata 布局，必须与 bpf/device_block.bpf.c 的 struct bpf_config 一致。
// 加载时会用 bpf_map__value_size() 复核，不一致直接拒绝加载。
struct BpfConfig {
    std::uint32_t device_major;
    std::uint32_t padding;
    std::uint64_t host_mnt_ns;
};

static_assert(sizeof(DeviceId) == 8);
static_assert(sizeof(CgroupMajorKey) == 16);
static_assert(sizeof(BpfConfig) == 16);

class ProcessLock {
public:
    ProcessLock();
    ~ProcessLock();

    ProcessLock(const ProcessLock&) = delete;
    auto operator=(const ProcessLock&) -> ProcessLock& = delete;

  private:
    int descriptor_;
};

void ensure_bpf_ready(const std::filesystem::path& object_path,
                      std::uint32_t device_major);
void validate_bpf_list_ready(const std::filesystem::path& object_path);
void validate_bpf_status_ready(const std::filesystem::path& object_path);
void require_bpf_ready();
void unload_bpf();
void validate_reservation_conflicts(
    std::uint64_t cgroup_id,
    const std::vector<DeviceId>& requested,
    const std::vector<std::pair<DeviceId, std::uint64_t>>& existing);
void reserve_devices(std::uint64_t cgroup_id,
                     const std::vector<DeviceId>& devices);
void release_all_devices(std::uint64_t cgroup_id);
void dump_bpf_maps(std::ostream& output);

// 容器归属登记: key = 容器 init 进程的 mount namespace inum。
// 容器不持有授权，登记只是把它挂到某个沙盒的授权上；沙盒销毁时一并清理。
void register_container_owner(std::uint64_t mount_namespace,
                              std::uint64_t cgroup_id);
void unregister_container_owner(std::uint64_t mount_namespace);
auto container_owner_of(std::uint64_t mount_namespace) -> std::optional<std::uint64_t>;
auto container_owners() -> std::vector<std::pair<std::uint64_t, std::uint64_t>>;

auto create_cgroup(std::string_view name, std::uint64_t cpu_count,
                   std::uint64_t memory_bytes) -> std::uint64_t;

// /proc/<pid>/ns/mnt 的 st_ino。与 BPF 里 CO-RE 读到的 ns.inum 是同一个数，
// 两侧不需要换算。
auto mount_namespace_of(pid_t pid) -> std::uint64_t;
void join_cgroup(std::string_view name, pid_t pid);
void destroy_cgroup(std::string_view name);
void show_cgroup_status(std::string_view name, std::ostream& output);
void show_cgroup_processes(std::string_view name, std::ostream& output);
auto cgroup_names() -> std::vector<std::string>;
auto state_names() -> std::vector<std::string>;
auto cgroup_exists(std::string_view name) -> bool;
auto cgroup_id(std::string_view name) -> std::uint64_t;

void validate_sandbox_name(std::string_view name);
void write_state_cgroup_id(std::string_view name, std::uint64_t cgroup_id);
auto read_state_cgroup_id(std::string_view name) -> std::uint64_t;
auto state_exists(std::string_view name) -> bool;
void remove_state(std::string_view name);
void remove_all_state();

}  // namespace neu_box::sandbox

#endif
