#include "sandbox.hpp"

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <exception>
#include <fstream>
#include <iostream>
#include <optional>
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

class UniqueFd {
public:
    explicit UniqueFd(int descriptor = -1) noexcept
        : descriptor_(descriptor) {}

    ~UniqueFd() {
        if (descriptor_ >= 0) {
            ::close(descriptor_);
        }
    }

    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;

    UniqueFd(UniqueFd&& other) noexcept
        : descriptor_(other.release()) {}

    UniqueFd& operator=(UniqueFd&& other) noexcept {
        if (this != &other) {
            reset(other.release());
        }
        return *this;
    }

    int get() const noexcept { return descriptor_; }

    int release() noexcept {
        const int descriptor = descriptor_;
        descriptor_ = -1;
        return descriptor;
    }

    void reset(int descriptor = -1) noexcept {
        if (descriptor_ >= 0) {
            ::close(descriptor_);
        }
        descriptor_ = descriptor;
    }

private:
    int descriptor_;
};

class BpfObject {
public:
    explicit BpfObject(bpf_object* object) : object_(object) {}
    ~BpfObject() {
        if (object_ != nullptr) {
            bpf_object__close(object_);
        }
    }

    BpfObject(const BpfObject&) = delete;
    BpfObject& operator=(const BpfObject&) = delete;

    bpf_object* get() const noexcept { return object_; }

private:
    bpf_object* object_;
};

struct PinnedMap {
    std::string_view name;
    std::string_view path;
    bpf_map_type type;
    std::uint32_t key_size;
    std::uint32_t value_size;
    std::uint32_t max_entries;
};

constexpr std::array<PinnedMap, 3> kPinnedMaps{{
    {"reserved_devices", kReservedDevicesPin, BPF_MAP_TYPE_HASH,
     sizeof(DeviceId), sizeof(std::uint64_t), 256},
    {"reserved_majors", kReservedMajorsPin, BPF_MAP_TYPE_HASH,
     sizeof(CgroupMajorKey), sizeof(std::uint8_t), 256},
    {"devdrv_major", kDevdrvMajorPin, BPF_MAP_TYPE_ARRAY,
     sizeof(std::uint32_t), sizeof(std::uint32_t), 1},
}};

[[noreturn]] void throw_errno(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

[[noreturn]] void throw_libbpf(int error, const std::string& operation) {
    const int code = error < 0 ? -error : error;
    throw std::system_error(code, std::generic_category(), operation);
}

std::string as_string(std::string_view value) {
    return std::string(value.data(), value.size());
}

UniqueFd open_pinned(std::string_view path) {
    const std::string value = as_string(path);
    const int descriptor = bpf_obj_get(value.c_str());
    if (descriptor < 0) {
        throw_errno("打开 pinned BPF 对象 " + value);
    }
    return UniqueFd(descriptor);
}

bool pin_exists(std::string_view path) {
    const std::string value = as_string(path);
    const int descriptor = bpf_obj_get(value.c_str());
    if (descriptor >= 0) {
        ::close(descriptor);
        return true;
    }
    if (errno == ENOENT) {
        return false;
    }
    throw_errno("检查 pinned BPF 对象 " + value);
}

UniqueFd open_root_cgroup() {
    const std::string root = as_string(kCgroupRoot);
    const int descriptor = ::open(root.c_str(), O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        throw_errno("打开 root cgroup");
    }
    return UniqueFd(descriptor);
}

std::uint32_t get_program_id(int program_descriptor) {
    bpf_prog_info information{};
    __u32 information_size = sizeof(information);
    if (bpf_obj_get_info_by_fd(program_descriptor, &information,
                               &information_size) != 0) {
        throw_errno("读取 BPF program ID");
    }
    return information.id;
}

std::array<__u8, BPF_TAG_SIZE> get_program_tag(
    int program_descriptor,
    std::string_view description) {
    bpf_prog_info information{};
    __u32 information_size = sizeof(information);
    if (bpf_obj_get_info_by_fd(program_descriptor, &information,
                               &information_size) != 0) {
        throw_errno("读取" + as_string(description) + " BPF program tag");
    }
    std::array<__u8, BPF_TAG_SIZE> tag{};
    std::copy(std::begin(information.tag), std::end(information.tag),
              tag.begin());
    return tag;
}

std::array<__u8, BPF_TAG_SIZE> expected_program_tag(
    const fs::path& object_path) {
    if (!fs::is_regular_file(object_path)) {
        throw std::runtime_error("缺少 BPF object: " + object_path.string());
    }

    const std::string path = object_path.string();
    bpf_object* raw_object = bpf_object__open_file(path.c_str(), nullptr);
    const long open_error = libbpf_get_error(raw_object);
    if (open_error != 0) {
        throw_libbpf(static_cast<int>(open_error),
                     "打开用于身份校验的 BPF object");
    }
    BpfObject object(raw_object);

    bpf_program* program =
        bpf_object__find_program_by_name(object.get(), "device_reserve");
    if (program == nullptr) {
        throw std::runtime_error(
            "BPF object 中没有用于身份校验的 device_reserve 程序");
    }
    const int load_error = bpf_object__load(object.get());
    if (load_error != 0) {
        throw_libbpf(load_error, "加载用于身份校验的 BPF object");
    }
    return get_program_tag(bpf_program__fd(program), "随包");
}

struct AttachedPrograms {
    std::vector<__u32> ids;
    __u32 flags;
};

AttachedPrograms attached_programs(int cgroup_descriptor) {
    std::vector<__u32> program_ids(16);
    while (true) {
        __u32 program_count = static_cast<__u32>(program_ids.size());
        __u32 attach_flags = 0;
        if (bpf_prog_query(cgroup_descriptor, BPF_CGROUP_DEVICE, 0,
                           &attach_flags, program_ids.data(),
            &program_count) == 0) {
            program_ids.resize(program_count);
            return {std::move(program_ids), attach_flags};
        }
        if (errno == ENOENT) {
            return {{}, 0};
        }
        if (errno != ENOSPC) {
            throw_errno("查询 cgroup device BPF 程序");
        }
        const std::size_t requested = static_cast<std::size_t>(program_count);
        const std::size_t next_size =
            requested > program_ids.size() ? requested : program_ids.size() * 2;
        if (next_size > 4096) {
            throw std::runtime_error("root cgroup 上挂载的 BPF 程序过多");
        }
        program_ids.resize(next_size);
    }
}

void validate_pinned_program(int program_descriptor,
                             const fs::path& object_path) {
    bpf_prog_info information{};
    __u32 information_size = sizeof(information);
    if (bpf_obj_get_info_by_fd(program_descriptor, &information,
                               &information_size) != 0) {
        throw_errno("读取 pinned BPF program 信息");
    }
    if (information.type != BPF_PROG_TYPE_CGROUP_DEVICE) {
        throw std::runtime_error("device_block pin 不是 cgroup device 程序");
    }
    const std::string name(
        reinterpret_cast<const char*>(information.name),
        strnlen(reinterpret_cast<const char*>(information.name),
                sizeof(information.name)));
    if (name != "device_reserve") {
        throw std::runtime_error("device_block pin 不是 device_reserve 程序");
    }
    const std::array<__u8, BPF_TAG_SIZE> pinned_tag =
        get_program_tag(program_descriptor, "pinned");
    if (pinned_tag != expected_program_tag(object_path)) {
        throw std::runtime_error(
            "pinned device_reserve 与随包 device_block.o 的 program tag "
            "不一致；拒绝复用旧版或未知 BPF 程序");
    }
}

std::set<std::uint32_t> referenced_map_ids(int program_descriptor) {
    bpf_prog_info information{};
    __u32 information_size = sizeof(information);
    if (bpf_obj_get_info_by_fd(program_descriptor, &information,
                               &information_size) != 0) {
        throw_errno("读取 pinned BPF program map 数量");
    }

    std::vector<__u32> map_ids(information.nr_map_ids);
    if (!map_ids.empty()) {
        bpf_prog_info with_map_ids{};
        with_map_ids.nr_map_ids = static_cast<__u32>(map_ids.size());
        with_map_ids.map_ids = static_cast<__u64>(
            reinterpret_cast<std::uintptr_t>(map_ids.data()));
        information_size = sizeof(with_map_ids);
        if (bpf_obj_get_info_by_fd(program_descriptor, &with_map_ids,
                                   &information_size) != 0) {
            throw_errno("读取 pinned BPF program map IDs");
        }
        if (with_map_ids.nr_map_ids > map_ids.size()) {
            throw std::runtime_error(
                "pinned BPF program 的 map 数量在查询期间发生变化");
        }
        map_ids.resize(with_map_ids.nr_map_ids);
    }
    return {map_ids.begin(), map_ids.end()};
}

// 检查 root cgroup 上所有同名 attachment 的归属，并返回预期程序是否已
// 挂载。仅凭 pin 本身不足以证明数据路径唯一：另一个 device_reserve 若以
// ALLOW_MULTI 方式并存，会让 load/join/cleanup 对实际执行链产生错误判断。
bool validate_device_reserve_attachments(
    int cgroup_descriptor,
    std::optional<std::uint32_t> expected_program_id) {
    const AttachedPrograms attached = attached_programs(cgroup_descriptor);
    if (!attached.ids.empty() && attached.flags != BPF_F_ALLOW_MULTI) {
        throw std::runtime_error(
            "root cgroup 的 device BPF attachment mode 不是 "
            "BPF_F_ALLOW_MULTI，子 cgroup 可能覆盖隔离程序");
    }
    std::size_t expected_count = 0;
    for (std::uint32_t program_id : attached.ids) {
        const int descriptor = bpf_prog_get_fd_by_id(program_id);
        if (descriptor < 0) {
            if (errno == ENOENT) {
                throw std::runtime_error(
                    "root cgroup 的 BPF attachment 在校验期间发生变化");
            }
            throw_errno("按 ID 打开 attached BPF program");
        }
        UniqueFd program(descriptor);

        bpf_prog_info information{};
        __u32 information_size = sizeof(information);
        if (bpf_obj_get_info_by_fd(program.get(), &information,
                                   &information_size) != 0) {
            throw_errno("读取 attached BPF program 信息");
        }
        const std::string name(
            reinterpret_cast<const char*>(information.name),
            strnlen(reinterpret_cast<const char*>(information.name),
                    sizeof(information.name)));
        if (information.type == BPF_PROG_TYPE_CGROUP_DEVICE &&
            name == "device_reserve") {
            if (expected_program_id.has_value() &&
                program_id == *expected_program_id) {
                ++expected_count;
                continue;
            }
            throw std::runtime_error(
                "root cgroup 上存在不属于当前 pin 的 device_reserve "
                "program (ID=" + std::to_string(program_id) + ")");
        }
    }
    if (expected_count > 1) {
        throw std::runtime_error(
            "当前 pinned device_reserve 在 root cgroup 上重复挂载");
    }
    return expected_count == 1;
}

enum class PinLayout {
    none,
    current,
    invalid,
};

PinLayout pinned_layout() {
    const bool program = pin_exists(kBpfProgramPin);
    const bool reserved_devices = pin_exists(kReservedDevicesPin);
    const bool reserved_majors = pin_exists(kReservedMajorsPin);
    const bool devdrv_major = pin_exists(kDevdrvMajorPin);

    if (!program && !reserved_devices && !reserved_majors && !devdrv_major) {
        return PinLayout::none;
    }
    if (program && reserved_devices && reserved_majors && devdrv_major) {
        return PinLayout::current;
    }
    return PinLayout::invalid;
}

bool any_pin_exists() {
    if (pin_exists(kBpfProgramPin)) {
        return true;
    }
    for (const PinnedMap& map : kPinnedMaps) {
        if (pin_exists(map.path)) {
            return true;
        }
    }
    return false;
}

void validate_pinned_objects(const fs::path& object_path) {
    UniqueFd program = open_pinned(kBpfProgramPin);
    validate_pinned_program(program.get(), object_path);

    std::set<std::uint32_t> pinned_map_ids;
    for (const PinnedMap& expected : kPinnedMaps) {
        UniqueFd map = open_pinned(expected.path);
        bpf_map_info information{};
        __u32 information_size = sizeof(information);
        if (bpf_obj_get_info_by_fd(map.get(), &information,
                                   &information_size) != 0) {
            throw_errno("读取 pinned map 信息: " + as_string(expected.name));
        }
        if (information.type != expected.type ||
            information.key_size != expected.key_size ||
            information.value_size != expected.value_size ||
            information.max_entries != expected.max_entries) {
            throw std::runtime_error("pinned map ABI 不匹配: " +
                                     as_string(expected.name));
        }
        const std::string name(
            reinterpret_cast<const char*>(information.name),
            strnlen(reinterpret_cast<const char*>(information.name),
                    sizeof(information.name)));
        // 内核的 BPF_OBJ_NAME_LEN 包含结尾 NUL；reserved_devices 等
        // 较长 ELF 名称会在 bpf_map_info.name 中截断为 15 字节。
        const std::string expected_kernel_name(
            expected.name.substr(0, sizeof(information.name) - 1));
        if (name != expected_kernel_name) {
            throw std::runtime_error("pinned map 名称不匹配: " +
                                     as_string(expected.name));
        }
        if (!pinned_map_ids.insert(information.id).second) {
            throw std::runtime_error("多个 pin 指向同一个 BPF map");
        }
    }

    const std::set<std::uint32_t> program_map_ids =
        referenced_map_ids(program.get());
    if (program_map_ids != pinned_map_ids) {
        throw std::runtime_error(
            "pinned BPF program 引用的 maps 与 sandbox_maps pins 不匹配");
    }
}

void validate_current_pins(PinLayout layout, const fs::path& object_path) {
    if (layout != PinLayout::current) {
        throw std::runtime_error("BPF pin 状态不是当前完整布局");
    }
    validate_pinned_objects(object_path);
}

std::uint32_t find_devdrv_major() {
    std::ifstream input("/proc/devices");
    if (!input) {
        throw std::runtime_error("无法读取 /proc/devices");
    }

    bool in_character_devices = false;
    std::string line;
    while (std::getline(input, line)) {
        if (line == "Character devices:") {
            in_character_devices = true;
            continue;
        }
        if (line == "Block devices:") {
            break;
        }
        if (!in_character_devices) {
            continue;
        }

        std::uint64_t major = 0;
        std::string driver;
        std::istringstream fields(line);
        if (fields >> major >> driver && driver == "devdrv-cdev") {
            if (major == 0 || major > UINT32_MAX) {
                throw std::runtime_error(
                    "devdrv-cdev major 超出 uint32 范围");
            }
            return static_cast<std::uint32_t>(major);
        }
    }
    return 0;
}

void remove_pin_files() {
    // program pin 最后删除。若中途失败，剩余对象会形成 invalid 布局；
    // 后续命令将拒绝自动处理，避免按程序名或部分 pin 猜测归属。
    for (const PinnedMap& map : kPinnedMaps) {
        const std::string path = as_string(map.path);
        std::error_code error;
        fs::remove(path, error);
        if (error) {
            throw std::system_error(error, "删除 BPF map pin " + path);
        }
    }

    const std::string map_directory = as_string(kBpfMapDirectory);
    std::error_code error;
    fs::remove(map_directory, error);
    if (error) {
        throw std::system_error(error,
                                "删除 BPF map pin 目录 " + map_directory);
    }

    const std::string program_path = as_string(kBpfProgramPin);
    fs::remove(program_path, error);
    if (error) {
        throw std::system_error(error,
                                "删除 BPF program pin " + program_path);
    }
}

void load_fresh_object(const fs::path& object_path) {
    if (!fs::is_regular_file(object_path)) {
        throw std::runtime_error("缺少 BPF object: " + object_path.string());
    }
    if (any_pin_exists()) {
        throw std::runtime_error(
            "检测到并发或意外创建的 BPF pin；请先人工确认并清理");
    }
    {
        UniqueFd cgroup = open_root_cgroup();
        validate_device_reserve_attachments(cgroup.get(), std::nullopt);
    }

    const std::string path = object_path.string();
    bpf_object* raw_object = bpf_object__open_file(path.c_str(), nullptr);
    const long open_error = libbpf_get_error(raw_object);
    if (open_error != 0) {
        throw_libbpf(static_cast<int>(open_error), "打开 BPF object");
    }
    BpfObject object(raw_object);

    const int load_error = bpf_object__load(object.get());
    if (load_error != 0) {
        throw_libbpf(load_error, "加载 BPF object");
    }

    bpf_program* program =
        bpf_object__find_program_by_name(object.get(), "device_reserve");
    if (program == nullptr) {
        throw std::runtime_error("BPF object 中没有 device_reserve 程序");
    }
    for (const PinnedMap& expected : kPinnedMaps) {
        if (bpf_object__find_map_by_name(object.get(),
                                         as_string(expected.name).c_str()) ==
            nullptr) {
            throw std::runtime_error("BPF object 中没有 map: " +
                                     as_string(expected.name));
        }
    }

    try {
        fs::create_directories(as_string(kBpfMapDirectory));
        int error = bpf_object__pin_maps(
            object.get(), as_string(kBpfMapDirectory).c_str());
        if (error != 0) {
            throw_libbpf(error, "pin BPF maps");
        }
        error = bpf_program__pin(
            program, as_string(kBpfProgramPin).c_str());
        if (error != 0) {
            throw_libbpf(error, "pin BPF program");
        }
        // ARRAY map 的初值是 0。必须先写入动态 major，再 attach，避免
        // 新程序以 fail-open 状态进入数据路径。
        configure_devdrv_major();

        UniqueFd cgroup = open_root_cgroup();
        if (bpf_prog_attach(bpf_program__fd(program), cgroup.get(),
                            BPF_CGROUP_DEVICE, BPF_F_ALLOW_MULTI) != 0) {
            throw_errno("挂载 cgroup device BPF program");
        }
    } catch (...) {
        // 此处进入前已确认没有任何预存 pin，所以可以清理 pin_maps
        // 可能在中途留下的部分结果。
        const std::exception_ptr load_exception = std::current_exception();
        try {
            remove_pin_files();
        } catch (const std::exception& cleanup_error) {
            std::string original = "未知错误";
            try {
                std::rethrow_exception(load_exception);
            } catch (const std::exception& error) {
                original = error.what();
            }
            throw std::runtime_error(
                "加载 BPF object 失败，且 pin 清理不完整；残留 pin 将被"
                "判为 invalid，需人工清理。原因: " +
                original + "; 清理: " + cleanup_error.what());
        }
        std::rethrow_exception(load_exception);
    }
}

template <typename Key, typename Value>
std::vector<std::pair<Key, Value>> read_map_entries(int descriptor) {
    std::vector<std::pair<Key, Value>> entries;
    std::optional<Key> current;
    while (true) {
        Key next{};
        if (bpf_map_get_next_key(descriptor,
                                 current ? &*current : nullptr,
                                 &next) != 0) {
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

}  // namespace

void configure_devdrv_major() {
    UniqueFd map = open_pinned(kDevdrvMajorPin);
    const std::uint32_t key = 0;
    const std::uint32_t major = find_devdrv_major();

    // 驱动重载可能让动态 major 发生变化。已有 Ascend 预留仍以旧 major
    // 为 key，直接改 map 会让现有隔离静默失效，因此必须先排空沙盒。
    UniqueFd reserved = open_pinned(kReservedDevicesPin);
    const auto reservations =
        read_map_entries<DeviceId, std::uint64_t>(reserved.get());
    for (const auto& reservation : reservations) {
        if (reservation.first.major == 195) {
            continue;
        }
        if (major == 0 || reservation.first.major != major) {
            throw std::runtime_error(
                "devdrv-cdev major 已缺失或变化，但仍有 Ascend 设备预留；"
                "请先销毁现有 sandbox 并执行 cleanup");
        }
    }

    if (bpf_map_update_elem(map.get(), &key, &major, BPF_ANY) != 0) {
        throw_errno("更新 devdrv_major map");
    }

    if (major == 0) {
        std::cerr << "[bpf] 未发现 devdrv-cdev，Ascend 隔离未启用\n";
    } else {
        std::cout << "[bpf] devdrv-cdev major: " << major << '\n';
    }
}

void ensure_bpf_ready(const fs::path& object_path) {
    const PinLayout layout = pinned_layout();
    if (layout == PinLayout::none) {
        if (!cgroup_names().empty()) {
            throw std::runtime_error(
                "存在 sandbox cgroup 但 BPF pin 已丢失，拒绝加载空 maps");
        }
        load_fresh_object(object_path);
        return;
    }
    validate_current_pins(layout, object_path);
    UniqueFd program = open_pinned(kBpfProgramPin);
    UniqueFd cgroup = open_root_cgroup();
    const bool attached = validate_device_reserve_attachments(
        cgroup.get(), get_program_id(program.get()));
    configure_devdrv_major();
    if (!attached &&
        bpf_prog_attach(program.get(), cgroup.get(), BPF_CGROUP_DEVICE,
                        BPF_F_ALLOW_MULTI) != 0) {
        throw_errno("重新挂载 cgroup device BPF program");
    }
}

void validate_bpf_cleanup_ready(const fs::path& object_path) {
    const PinLayout layout = pinned_layout();
    if (layout == PinLayout::none) {
        if (!cgroup_names().empty()) {
            throw std::runtime_error(
                "存在 sandbox cgroup 但 BPF pin 已丢失，拒绝自动清理");
        }
        UniqueFd cgroup = open_root_cgroup();
        validate_device_reserve_attachments(cgroup.get(), std::nullopt);
        return;
    }
    validate_current_pins(layout, object_path);
    UniqueFd program = open_pinned(kBpfProgramPin);
    UniqueFd cgroup = open_root_cgroup();
    validate_device_reserve_attachments(
        cgroup.get(), get_program_id(program.get()));
}

void validate_bpf_join_ready(const fs::path& object_path) {
    const PinLayout layout = pinned_layout();
    validate_current_pins(layout, object_path);

    UniqueFd program = open_pinned(kBpfProgramPin);
    UniqueFd cgroup = open_root_cgroup();
    if (!validate_device_reserve_attachments(
            cgroup.get(), get_program_id(program.get()))) {
        throw std::runtime_error(
            "当前 BPF program 未挂载，拒绝将进程加入 sandbox");
    }
    configure_devdrv_major();
}

void validate_bpf_status_ready(const fs::path& object_path) {
    const PinLayout layout = pinned_layout();
    validate_current_pins(layout, object_path);

    UniqueFd program = open_pinned(kBpfProgramPin);
    UniqueFd cgroup = open_root_cgroup();
    if (!validate_device_reserve_attachments(
            cgroup.get(), get_program_id(program.get()))) {
        throw std::runtime_error(
            "当前 BPF program 未挂载，sandbox 状态不可信");
    }

    UniqueFd devdrv = open_pinned(kDevdrvMajorPin);
    const std::uint32_t key = 0;
    std::uint32_t configured_major = 0;
    if (bpf_map_lookup_elem(devdrv.get(), &key, &configured_major) != 0) {
        throw_errno("读取 devdrv_major map");
    }
    const std::uint32_t current_major = find_devdrv_major();
    if (configured_major != current_major) {
        throw std::runtime_error(
            "devdrv_major map 与 /proc/devices 中 devdrv-cdev 不一致: map=" +
            std::to_string(configured_major) + ", current=" +
            std::to_string(current_major));
    }
}

void validate_bpf_list_ready(const fs::path& object_path) {
    const PinLayout layout = pinned_layout();
    if (layout == PinLayout::none) {
        if (!fs::is_regular_file(object_path)) {
            throw std::runtime_error("缺少 BPF object: " +
                                     object_path.string());
        }
        if (!cgroup_names().empty()) {
            throw std::runtime_error(
                "存在 sandbox cgroup 但 BPF pin 已丢失，状态不可信");
        }
        UniqueFd cgroup = open_root_cgroup();
        validate_device_reserve_attachments(cgroup.get(), std::nullopt);
        return;
    }
    validate_bpf_status_ready(object_path);
}

void unload_bpf(const fs::path& object_path) {
    const PinLayout layout = pinned_layout();
    if (layout == PinLayout::none) {
        UniqueFd cgroup = open_root_cgroup();
        validate_device_reserve_attachments(cgroup.get(), std::nullopt);
        return;
    }
    validate_current_pins(layout, object_path);
    UniqueFd program = open_pinned(kBpfProgramPin);
    UniqueFd cgroup = open_root_cgroup();
    const bool attached = validate_device_reserve_attachments(
        cgroup.get(), get_program_id(program.get()));
    if (attached &&
        bpf_prog_detach2(program.get(), cgroup.get(), BPF_CGROUP_DEVICE) != 0 &&
        errno != ENOENT) {
        throw_errno("分离 pinned device_reserve BPF program");
    }
    remove_pin_files();
}

void reserve_devices(std::uint64_t cgroup_id_value,
                     const std::vector<DeviceId>& devices) {
    bool has_ascend_device = false;
    for (const DeviceId& device : devices) {
        if (device.major != 195) {
            has_ascend_device = true;
            break;
        }
    }
    if (has_ascend_device) {
        UniqueFd devdrv = open_pinned(kDevdrvMajorPin);
        const std::uint32_t key = 0;
        std::uint32_t configured_major = 0;
        if (bpf_map_lookup_elem(devdrv.get(), &key, &configured_major) != 0) {
            throw_errno("读取 devdrv_major map");
        }
        if (configured_major == 0) {
            throw std::runtime_error(
                "未发现 devdrv-cdev，拒绝创建声明 Ascend 设备的 sandbox");
        }
        for (const DeviceId& device : devices) {
            if (device.major != 195 && device.major != configured_major) {
                throw std::runtime_error(
                    "设备 major " + std::to_string(device.major) +
                    " 与 devdrv-cdev 当前 major " +
                    std::to_string(configured_major) + " 不一致");
            }
        }
    }

    UniqueFd reserved = open_pinned(kReservedDevicesPin);
    const auto existing_reservations =
        read_map_entries<DeviceId, std::uint64_t>(reserved.get());
    validate_reservation_conflicts(
        cgroup_id_value, devices, existing_reservations);

    UniqueFd majors = open_pinned(kReservedMajorsPin);
    struct PreviousDevice {
        DeviceId key;
        bool existed;
        std::uint64_t owner;
    };
    struct PreviousMajor {
        CgroupMajorKey key;
        bool existed;
        std::uint8_t value;
    };
    std::vector<PreviousDevice> previous_devices;
    std::vector<PreviousMajor> previous_majors;
    std::set<std::uint32_t> seen_majors;

    try {
        for (const DeviceId& device : devices) {
            std::uint64_t old_owner = 0;
            const bool existed =
                bpf_map_lookup_elem(reserved.get(), &device, &old_owner) == 0;
            if (!existed && errno != ENOENT) {
                throw_errno("读取 reserved_devices");
            }
            previous_devices.push_back({device, existed, old_owner});

            // 冲突已在任何 map 变更之前完成全量检查。这里只允许创建新
            // 条目或由同一 owner 幂等刷新，不能偷占另一个 cgroup 的设备。
            if (existed && old_owner != cgroup_id_value) {
                throw std::runtime_error(
                    "设备预留在写入前发生并发变化: " +
                    std::to_string(device.major) + ':' +
                    (device.minor == kMinorWildcard
                         ? std::string("*")
                         : std::to_string(device.minor)));
            }
            if (bpf_map_update_elem(reserved.get(), &device,
                                    &cgroup_id_value, BPF_ANY) != 0) {
                throw_errno("写入 reserved_devices");
            }

            if (!seen_majors.insert(device.major).second) {
                continue;
            }
            const CgroupMajorKey key{cgroup_id_value, device.major, 0};
            std::uint8_t old_value = 0;
            const bool major_existed =
                bpf_map_lookup_elem(majors.get(), &key, &old_value) == 0;
            if (!major_existed && errno != ENOENT) {
                throw_errno("读取 reserved_majors");
            }
            previous_majors.push_back({key, major_existed, old_value});

            const std::uint8_t present = 1;
            if (bpf_map_update_elem(majors.get(), &key, &present,
                                    BPF_ANY) != 0) {
                throw_errno("写入 reserved_majors");
            }
        }
    } catch (...) {
        for (auto iterator = previous_devices.rbegin();
             iterator != previous_devices.rend(); ++iterator) {
            if (iterator->existed) {
                bpf_map_update_elem(reserved.get(), &iterator->key,
                                    &iterator->owner, BPF_ANY);
            } else {
                bpf_map_delete_elem(reserved.get(), &iterator->key);
            }
        }
        for (auto iterator = previous_majors.rbegin();
             iterator != previous_majors.rend(); ++iterator) {
            if (iterator->existed) {
                bpf_map_update_elem(majors.get(), &iterator->key,
                                    &iterator->value, BPF_ANY);
            } else {
                bpf_map_delete_elem(majors.get(), &iterator->key);
            }
        }
        throw;
    }
}

void release_all_devices(std::uint64_t cgroup_id_value) {
    const PinLayout layout = pinned_layout();
    if (layout == PinLayout::none) {
        return;
    }
    // 调用方在持有全局 ProcessLock 时已完成随包 object、attachment 和
    // pin 的完整校验；这里再次核验布局，防止清理过程中遇到部分 pin。
    if (layout != PinLayout::current) {
        throw std::runtime_error("BPF pin 状态不是当前完整布局");
    }

    UniqueFd reserved = open_pinned(kReservedDevicesPin);
    const auto device_entries =
        read_map_entries<DeviceId, std::uint64_t>(reserved.get());
    for (const auto& entry : device_entries) {
        if (entry.second == cgroup_id_value &&
            bpf_map_delete_elem(reserved.get(), &entry.first) != 0 &&
            errno != ENOENT) {
            throw_errno("按 owner 删除 reserved_devices 条目");
        }
    }

    UniqueFd majors = open_pinned(kReservedMajorsPin);
    const auto major_entries =
        read_map_entries<CgroupMajorKey, std::uint8_t>(majors.get());
    for (const auto& entry : major_entries) {
        if (entry.first.cgroup_id == cgroup_id_value &&
            bpf_map_delete_elem(majors.get(), &entry.first) != 0 &&
            errno != ENOENT) {
            throw_errno("按 owner 删除 reserved_majors 条目");
        }
    }
}

void dump_bpf_maps(std::ostream& output) {
    output << "  [reserved_devices]\n";
    if (!pin_exists(kReservedDevicesPin)) {
        output << "    (未加载)\n";
    } else {
        UniqueFd reserved = open_pinned(kReservedDevicesPin);
        const auto entries =
            read_map_entries<DeviceId, std::uint64_t>(reserved.get());
        if (entries.empty()) {
            output << "    (无)\n";
        }
        for (const auto& entry : entries) {
            output << "    " << entry.first.major << ':';
            if (entry.first.minor == kMinorWildcard) {
                output << '*';
            } else {
                output << entry.first.minor;
            }
            output << " -> cgroup " << entry.second << '\n';
        }
    }

    output << "  [reserving_cgroups]\n";
    if (!pin_exists(kReservedMajorsPin)) {
        output << "    (未加载)\n";
    } else {
        UniqueFd majors = open_pinned(kReservedMajorsPin);
        const auto entries =
            read_map_entries<CgroupMajorKey, std::uint8_t>(majors.get());
        if (entries.empty()) {
            output << "    (无)\n";
        }
        for (const auto& entry : entries) {
            output << "    cgroup " << entry.first.cgroup_id
                   << ", major " << entry.first.major << " -> "
                   << static_cast<unsigned int>(entry.second) << '\n';
        }
    }
}

}  // namespace neu_box::sandbox
