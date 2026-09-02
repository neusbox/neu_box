#ifndef NEU_BOX_NATIVE_SANDBOX_HPP
#define NEU_BOX_NATIVE_SANDBOX_HPP

#include <cstdint>
#include <filesystem>
#include <iosfwd>
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
inline constexpr std::string_view kDevdrvMajorPin =
    "/sys/fs/bpf/sandbox_maps/devdrv_major";

inline constexpr std::string_view kLockFile =
    "/run/neu-box/sandbox.lock";
inline constexpr std::uint32_t kMinorWildcard = UINT32_MAX;

struct DeviceId {
    std::uint32_t major;
    std::uint32_t minor;
};

struct CgroupMajorKey {
    std::uint64_t cgroup_id;
    std::uint32_t major;
    std::uint32_t padding;
};

static_assert(sizeof(DeviceId) == 8);
static_assert(sizeof(CgroupMajorKey) == 16);

class ProcessLock {
public:
    ProcessLock();
    ~ProcessLock();

    ProcessLock(const ProcessLock&) = delete;
    ProcessLock& operator=(const ProcessLock&) = delete;

    void release();

private:
    int descriptor_ = -1;
};

void ensure_bpf_ready(const std::filesystem::path& object_path);
void validate_bpf_cleanup_ready(
    const std::filesystem::path& object_path);
void validate_bpf_join_ready(const std::filesystem::path& object_path);
void validate_bpf_status_ready(const std::filesystem::path& object_path);
void validate_bpf_list_ready(const std::filesystem::path& object_path);
void unload_bpf(const std::filesystem::path& object_path);
void configure_devdrv_major();
void validate_reservation_conflicts(
    std::uint64_t cgroup_id,
    const std::vector<DeviceId>& requested,
    const std::vector<std::pair<DeviceId, std::uint64_t>>& existing);
void reserve_devices(std::uint64_t cgroup_id,
                     const std::vector<DeviceId>& devices);
void release_all_devices(std::uint64_t cgroup_id);
void dump_bpf_maps(std::ostream& output);

std::uint64_t create_cgroup(std::string_view name,
                            std::uint64_t cpu_count,
                            std::uint64_t memory_bytes);
void join_cgroup(std::string_view name, pid_t pid);
void destroy_cgroup(std::string_view name);
void show_cgroup_status(std::string_view name, std::ostream& output);
void show_cgroup_processes(std::string_view name, std::ostream& output);
std::vector<std::string> cgroup_names();
bool cgroup_exists(std::string_view name);
std::uint64_t cgroup_id(std::string_view name);

void validate_sandbox_name(std::string_view name);
void write_state_cgroup_id(std::string_view name, std::uint64_t cgroup_id);
std::uint64_t read_state_cgroup_id(std::string_view name);
bool state_exists(std::string_view name);
void remove_state(std::string_view name);
void remove_all_state();

}  // namespace neu_box::sandbox

#endif
