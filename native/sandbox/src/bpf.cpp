#include "sandbox.hpp"

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <filesystem>
#include <fstream>
#include <memory>
#include <optional>
#include <ostream>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace neu_box::sandbox {
namespace {

namespace fs = std::filesystem;

class Fd {
  public:
    explicit Fd(int value) : value_(value) {}
    ~Fd() {
        ::close(value_);
    }

    Fd(const Fd&) = delete;
    auto operator=(const Fd&) -> Fd& = delete;

    auto get() const -> int {
        return value_;
    }

  private:
    int value_;
};

using BpfObject = std::unique_ptr<bpf_object, decltype(&bpf_object__close)>;

struct MapPin {
    const char* name;
    std::string_view path;
};

constexpr std::array<MapPin, 3> kMaps{{
    {"reserved_devices", kReservedDevicesPin},
    {"reserved_majors", kReservedMajorsPin},
    {"container_owner", kContainerOwnerPin},
}};

// Kept as a diagnostic helper for callers that need to discover the default
// accelerator major.  Normal load paths pass an explicit major from Python so
// that a configured CPU-only installation can deliberately use zero.
[[maybe_unused]]
auto discover_devdrv_major() -> std::optional<std::uint32_t> {
    std::ifstream devices("/proc/devices");
    std::string line;
    while (std::getline(devices, line)) {
        std::istringstream fields(line);
        std::uint32_t major = 0;
        std::string driver;
        if (!(fields >> major >> driver)) {
            continue;
        }
        if (driver == "devdrv-cdev") {
            return major;
        }
    }
    return std::nullopt;
}

void configure_device_block_object(bpf_object* object, const BpfConfig& values);

[[noreturn]]
void throw_errno(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

void check_libbpf(int result, const std::string& operation) {
    if (result != 0) {
        throw std::system_error(result < 0 ? -result : result, std::generic_category(), operation);
    }
}

auto path(std::string_view value) -> fs::path {
    return fs::path(std::string(value));
}

auto pin_exists(std::string_view pin) -> bool {
    return fs::exists(path(pin));
}

auto open_pin(std::string_view pin) -> Fd {
    const std::string value(pin);
    const int descriptor = bpf_obj_get(value.c_str());
    if (descriptor < 0) {
        throw_errno("打开 pinned BPF 对象 " + value);
    }
    return Fd(descriptor);
}

auto open_root_cgroup() -> Fd {
    const int descriptor = ::open(path(kCgroupRoot).c_str(), O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        throw_errno("打开 root cgroup");
    }
    return Fd(descriptor);
}

auto program_id(int descriptor) -> std::uint32_t {
    bpf_prog_info info{};
    __u32 size = sizeof(info);
    if (bpf_obj_get_info_by_fd(descriptor, &info, &size) != 0) {
        throw_errno("读取 BPF program ID");
    }
    return info.id;
}

auto attached_program_ids(int cgroup) -> std::vector<__u32> {
    std::vector<__u32> ids(16);
    while (true) {
        __u32 count = static_cast<__u32>(ids.size());
        __u32 flags = 0;
        if (bpf_prog_query(cgroup, BPF_CGROUP_DEVICE, 0, &flags, ids.data(), &count) == 0) {
            ids.resize(count);
            return ids;
        }
        if (errno == ENOENT) {
            return {};
        }
        if (errno != ENOSPC) {
            throw_errno("查询 cgroup device BPF 程序");
        }
        ids.resize(count);
    }
}

auto is_attached(int cgroup, int program) -> bool {
    const auto ids = attached_program_ids(cgroup);
    return std::find(ids.begin(), ids.end(), program_id(program)) != ids.end();
}

auto maps_ready() -> bool {
    return std::all_of(kMaps.begin(), kMaps.end(),
                       [](const MapPin& map) { return pin_exists(map.path); });
}

auto pins_ready() -> bool {
    return pin_exists(kBpfProgramPin) && maps_ready();
}

auto any_pin_exists() -> bool {
    if (pin_exists(kBpfProgramPin)) {
        return true;
    }
    return std::any_of(kMaps.begin(), kMaps.end(),
                       [](const MapPin& map) { return pin_exists(map.path); });
}

void remove_pins() {
    fs::remove(path(kBpfProgramPin));
    for (const MapPin& map : kMaps) {
        fs::remove(path(map.path));
    }
    fs::remove(path(kBpfMapDirectory));
}

auto open_object(const fs::path& object_path) -> BpfObject {
    if (!fs::is_regular_file(object_path)) {
        throw std::runtime_error("缺少 BPF object: " + object_path.string());
    }
    bpf_object* raw = bpf_object__open_file(object_path.c_str(), nullptr);
    const long error = libbpf_get_error(raw);
    if (error != 0) {
        check_libbpf(static_cast<int>(error), "打开 BPF object");
    }
    return BpfObject(raw, bpf_object__close);
}

auto expected_program_tag(const fs::path& object_path) -> std::array<__u8, BPF_TAG_SIZE> {
    // Ask the kernel for the tag of the packaged object instead of hashing
    // the ELF file: the program tag is defined over loaded BPF instructions.
    BpfObject object = open_object(object_path);
    bpf_program* program = bpf_object__find_program_by_name(object.get(), "device_reserve");
    if (program == nullptr) {
        throw std::runtime_error("BPF object 中没有 device_reserve");
    }
    check_libbpf(bpf_object__load(object.get()), "加载 BPF object 以读取 tag");
    bpf_prog_info information{};
    __u32 size = sizeof(information);
    check_libbpf(bpf_obj_get_info_by_fd(bpf_program__fd(program), &information, &size),
                 "读取 BPF program 信息");
    std::array<__u8, BPF_TAG_SIZE> tag{};
    std::copy(std::begin(information.tag), std::end(information.tag), tag.begin());
    return tag;
}

void validate_pinned_program(int descriptor, const fs::path& object_path) {
    bpf_prog_info information{};
    __u32 size = sizeof(information);
    check_libbpf(bpf_obj_get_info_by_fd(descriptor, &information, &size),
                 "读取 pinned BPF program 信息");
    std::array<__u8, BPF_TAG_SIZE> pinned_tag{};
    std::copy(std::begin(information.tag), std::end(information.tag), pinned_tag.begin());
    if (pinned_tag != expected_program_tag(object_path)) {
        throw std::runtime_error("pinned BPF program 与 RPM 中的 object 不匹配");
    }
}

auto validate_device_reserve_attachments(
    int cgroup, std::optional<std::uint32_t> expected_program_id = {}) -> bool {
    std::vector<__u32> ids(16);
    __u32 flags = 0;
    __u32 count = static_cast<__u32>(ids.size());
    if (bpf_prog_query(cgroup, BPF_CGROUP_DEVICE, 0, &flags, ids.data(), &count) != 0) {
        if (errno == ENOENT) {
            return false;
        }
        throw_errno("查询 cgroup device BPF 附着");
    }
    ids.resize(count);
    struct AttachmentInfo {
        __u32 flags;
    } attached{flags};
    if (attached.flags != BPF_F_ALLOW_MULTI) {
        throw std::runtime_error("cgroup device BPF 附着模式不安全");
    }
    if (!expected_program_id.has_value()) {
        return !ids.empty();
    }
    return std::find(ids.begin(), ids.end(), *expected_program_id) != ids.end();
}

void configure_device_block_object(bpf_object* object, const BpfConfig& values) {
    bpf_map* rodata = nullptr;
    bpf_map* map = nullptr;
    bpf_object__for_each_map(map, object) {
        const std::string_view name(bpf_map__name(map));
        if (name.size() >= 7 && name.substr(name.size() - 7) == ".rodata") {
            rodata = map;
            break;
        }
    }
    if (rodata == nullptr) {
        throw std::runtime_error("BPF object 中没有 .rodata");
    }
    // .rodata 整块写入。先复核尺寸: C 侧的 struct bpf_config 与这里的
    // BpfConfig 一旦漂移，host_mnt_ns 会落到错误偏移上，判定随之失准。
    const std::uint32_t size = bpf_map__value_size(rodata);
    if (size != sizeof(BpfConfig)) {
        throw std::runtime_error("BPF .rodata 布局与 native CLI 不一致: " + std::to_string(size) +
                                 " != " + std::to_string(sizeof(BpfConfig)));
    }
    check_libbpf(bpf_map__set_initial_value(rodata, &values, sizeof(values)),
                 "设置 BPF 运行期配置");
}

void load_fresh_object(const fs::path& object_path, std::uint32_t device_major) {
    BpfObject object = open_object(object_path);
    BpfConfig values{};
    values.device_major = device_major;
    values.padding = 0;
    // 加载者自己的 mount namespace 就是判定里的"宿主机": CLI 以宿主身份
    // 运行（它本来就需要 root 与 cgroup 写权限），容器里的进程拿不到它。
    values.host_mnt_ns = mount_namespace_of(::getpid());
    configure_device_block_object(object.get(), values);

    bpf_program* program = bpf_object__find_program_by_name(object.get(), "device_reserve");
    if (program == nullptr) {
        throw std::runtime_error("BPF object 中没有 device_reserve");
    }
    for (const MapPin& expected : kMaps) {
        if (bpf_object__find_map_by_name(object.get(), expected.name) == nullptr) {
            throw std::runtime_error("BPF object 中没有 map: " + std::string(expected.name));
        }
    }

    check_libbpf(bpf_object__load(object.get()), "加载 BPF object");

    bool attached = false;
    try {
        fs::create_directories(path(kBpfMapDirectory));
        for (const MapPin& expected : kMaps) {
            bpf_map* map = bpf_object__find_map_by_name(object.get(), expected.name);
            check_libbpf(bpf_map__pin(map, path(expected.path).c_str()),
                         "pin BPF map " + std::string(expected.name));
        }
        check_libbpf(bpf_program__pin(program, path(kBpfProgramPin).c_str()), "pin BPF program");

        Fd cgroup = open_root_cgroup();
        if (bpf_prog_attach(bpf_program__fd(program), cgroup.get(), BPF_CGROUP_DEVICE,
                            BPF_F_ALLOW_MULTI) != 0) {
            throw_errno("挂载 cgroup device BPF program");
        }
        attached = true;
    } catch (...) {
        try {
            if (attached) {
                Fd rollback_cgroup = open_root_cgroup();
                if (bpf_prog_detach2(bpf_program__fd(program), rollback_cgroup.get(),
                                     BPF_CGROUP_DEVICE) != 0 &&
                    errno != ENOENT) {
                    throw_errno("回滚 cgroup device BPF program");
                }
            }
            remove_pins();
        } catch (const std::exception& rollback_error) {
            throw std::runtime_error("BPF 加载失败且回滚失败: " +
                                     std::string(rollback_error.what()));
        }
        throw;
    }
}

template <typename Key, typename Value>
auto read_entries(int descriptor) -> std::vector<std::pair<Key, Value>> {
    std::vector<std::pair<Key, Value>> entries;
    std::optional<Key> current;
    while (true) {
        Key next{};
        if (bpf_map_get_next_key(descriptor, current ? &*current : nullptr, &next) != 0) {
            if (errno == ENOENT) {
                return entries;
            }
            throw_errno("遍历 BPF map");
        }

        Value value{};
        if (bpf_map_lookup_elem(descriptor, &next, &value) == 0) {
            entries.emplace_back(next, value);
        } else if (errno != ENOENT) {
            throw_errno("读取 BPF map");
        }
        current = next;
    }
}

} // namespace

void validate_bpf_status_ready(const fs::path& object_path) {
    if (!pins_ready()) {
        throw std::runtime_error("BPF 程序尚未加载");
    }
    Fd program = open_pin(kBpfProgramPin);
    validate_pinned_program(program.get(), object_path);
    Fd cgroup = open_root_cgroup();
    if (!validate_device_reserve_attachments(cgroup.get(), program_id(program.get()))) {
        throw std::runtime_error("BPF 程序尚未挂载");
    }
}

void validate_bpf_list_ready(const fs::path& object_path) {
    if (!any_pin_exists() && cgroup_names().empty()) {
        return;
    }
    validate_bpf_status_ready(object_path);
}

void ensure_bpf_ready(const fs::path& object_path, std::uint32_t device_major) {
    if (pins_ready()) {
        Fd program = open_pin(kBpfProgramPin);
        // Reusing pins across an RPM replacement is safe only when they came
        // from the exact packaged BPF object.  A stale program can otherwise
        // survive pause/setup and enforce an obsolete map/layout forever.
        validate_pinned_program(program.get(), object_path);
        Fd cgroup = open_root_cgroup();
        if (!is_attached(cgroup.get(), program.get()) &&
            bpf_prog_attach(program.get(), cgroup.get(), BPF_CGROUP_DEVICE, BPF_F_ALLOW_MULTI) !=
                0) {
            throw_errno("重新挂载 cgroup device BPF program");
        }
        return;
    }
    if (any_pin_exists()) {
        throw std::runtime_error("BPF pin 不完整，请先执行 cleanup");
    }
    if (!cgroup_names().empty()) {
        throw std::runtime_error("存在 sandbox cgroup，但 BPF maps 已丢失");
    }
    load_fresh_object(object_path, device_major);
}

void require_bpf_ready() {
    if (!pins_ready()) {
        throw std::runtime_error("BPF 程序尚未加载");
    }
    Fd program = open_pin(kBpfProgramPin);
    Fd cgroup = open_root_cgroup();
    if (!is_attached(cgroup.get(), program.get())) {
        throw std::runtime_error("BPF 程序尚未挂载");
    }
}

void unload_bpf() {
    Fd cgroup = open_root_cgroup();
    auto attached = attached_program_ids(cgroup.get());
    if (pin_exists(kBpfProgramPin)) {
        Fd program = open_pin(kBpfProgramPin);
        const auto id = program_id(program.get());
        if (std::find(attached.begin(), attached.end(), id) != attached.end() &&
            bpf_prog_detach2(program.get(), cgroup.get(), BPF_CGROUP_DEVICE) != 0 &&
            errno != ENOENT) {
            throw_errno("分离 cgroup device BPF program");
        }
        attached = attached_program_ids(cgroup.get());
    }
    // 没有 program pin，或 detach 后仍有其它附着程序时，无法确认程序
    // 属于本实例；不能为了让 cleanup 返回成功而卸载未知程序。
    if (!attached.empty()) {
        throw std::runtime_error("仍有未能确认归属的 cgroup device BPF program，保留 pin 和隔离");
    }
    remove_pins();
}

void reserve_devices(std::uint64_t cgroup_id_value, const std::vector<DeviceId>& devices) {
    if (devices.empty()) {
        return;
    }

    Fd reserved = open_pin(kReservedDevicesPin);
    const auto existing = read_entries<DeviceId, std::uint64_t>(reserved.get());
    validate_reservation_conflicts(cgroup_id_value, devices, existing);

    Fd majors = open_pin(kReservedMajorsPin);
    std::set<std::uint32_t> seen_majors;
    for (const DeviceId& device : devices) {
        if (bpf_map_update_elem(reserved.get(), &device, &cgroup_id_value, BPF_ANY) != 0) {
            throw_errno("写入 reserved_devices");
        }
        if (!seen_majors.insert(device.major).second) {
            continue;
        }
        const CgroupMajorKey key{cgroup_id_value, device.major, 0};
        const std::uint8_t present = 1;
        if (bpf_map_update_elem(majors.get(), &key, &present, BPF_ANY) != 0) {
            throw_errno("写入 reserved_majors");
        }
    }
}

void release_all_devices(std::uint64_t cgroup_id_value) {
    if (!pin_exists(kReservedDevicesPin) && !pin_exists(kReservedMajorsPin) &&
        !pin_exists(kContainerOwnerPin)) {
        return;
    }

    Fd reserved = open_pin(kReservedDevicesPin);
    for (const auto& entry : read_entries<DeviceId, std::uint64_t>(reserved.get())) {
        if (entry.second == cgroup_id_value &&
            bpf_map_delete_elem(reserved.get(), &entry.first) != 0 && errno != ENOENT) {
            throw_errno("删除 reserved_devices 条目");
        }
    }

    Fd majors = open_pin(kReservedMajorsPin);
    for (const auto& entry : read_entries<CgroupMajorKey, std::uint8_t>(majors.get())) {
        if (entry.first.cgroup_id == cgroup_id_value &&
            bpf_map_delete_elem(majors.get(), &entry.first) != 0 && errno != ENOENT) {
            throw_errno("删除 reserved_majors 条目");
        }
    }

    // 委托方沙盒没了，挂在它名下的容器归属同时失效: 容器不持有授权，
    // 残留条目会在 mnt ns inum 被内核复用时把授权白送给后来的容器。
    if (pin_exists(kContainerOwnerPin)) {
        Fd owners = open_pin(kContainerOwnerPin);
        for (const auto& entry : read_entries<std::uint64_t, std::uint64_t>(owners.get())) {
            if (entry.second == cgroup_id_value &&
                bpf_map_delete_elem(owners.get(), &entry.first) != 0 && errno != ENOENT) {
                throw_errno("删除 container_owner 条目");
            }
        }
    }
}

void register_container_owner(std::uint64_t mount_namespace, std::uint64_t cgroup_id_value) {
    if (mount_namespace == 0) {
        throw std::invalid_argument("mount namespace 不能为 0");
    }
    Fd owners = open_pin(kContainerOwnerPin);
    if (bpf_map_update_elem(owners.get(), &mount_namespace, &cgroup_id_value, BPF_ANY) != 0) {
        throw_errno("写入 container_owner");
    }
}

void unregister_container_owner(std::uint64_t mount_namespace) {
    if (!pin_exists(kContainerOwnerPin)) {
        return;
    }
    Fd owners = open_pin(kContainerOwnerPin);
    if (bpf_map_delete_elem(owners.get(), &mount_namespace) != 0 && errno != ENOENT) {
        throw_errno("删除 container_owner 条目");
    }
}

auto container_owner_of(std::uint64_t mount_namespace) -> std::optional<std::uint64_t> {
    if (!pin_exists(kContainerOwnerPin)) {
        return std::nullopt;
    }
    Fd owners = open_pin(kContainerOwnerPin);
    std::uint64_t owner = 0;
    if (bpf_map_lookup_elem(owners.get(), &mount_namespace, &owner) != 0) {
        if (errno == ENOENT) {
            return std::nullopt;
        }
        throw_errno("读取 container_owner");
    }
    return owner;
}

auto container_owners() -> std::vector<std::pair<std::uint64_t, std::uint64_t>> {
    if (!pin_exists(kContainerOwnerPin)) {
        return {};
    }
    Fd owners = open_pin(kContainerOwnerPin);
    return read_entries<std::uint64_t, std::uint64_t>(owners.get());
}

void dump_bpf_maps(std::ostream& output) {
    output << "  [reserved_devices]\n";
    if (!pin_exists(kReservedDevicesPin)) {
        output << "    (未加载)\n";
    } else {
        Fd reserved = open_pin(kReservedDevicesPin);
        const auto entries = read_entries<DeviceId, std::uint64_t>(reserved.get());
        if (entries.empty()) {
            output << "    (无)\n";
        }
        for (const auto& entry : entries) {
            output << "    " << entry.first.major << ':' << entry.first.minor << " -> cgroup "
                   << entry.second << '\n';
        }
    }

    output << "  [reserving_cgroups]\n";
    if (!pin_exists(kReservedMajorsPin)) {
        output << "    (未加载)\n";
    } else {
        Fd majors = open_pin(kReservedMajorsPin);
        const auto entries = read_entries<CgroupMajorKey, std::uint8_t>(majors.get());
        if (entries.empty()) {
            output << "    (无)\n";
        }
        for (const auto& entry : entries) {
            output << "    cgroup " << entry.first.cgroup_id << ", major " << entry.first.major
                   << " -> " << static_cast<unsigned int>(entry.second) << '\n';
        }
    }

    output << "  [container_owner]\n";
    if (!pin_exists(kContainerOwnerPin)) {
        output << "    (未加载)\n";
    } else {
        const auto entries = container_owners();
        if (entries.empty()) {
            output << "    (无)\n";
        }
        for (const auto& entry : entries) {
            output << "    mnt ns " << entry.first << " -> cgroup " << entry.second << '\n';
        }
    }
}

} // namespace neu_box::sandbox
