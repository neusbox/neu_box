#include "sandbox.hpp"

#include <sstream>
#include <stdexcept>
#include <string>

namespace neu_box::sandbox {
namespace {

std::string device_text(const DeviceId& device) {
    std::ostringstream output;
    output << device.major << ':';
    if (device.minor == kMinorWildcard) {
        output << '*';
    } else {
        output << device.minor;
    }
    return output.str();
}

[[noreturn]] void throw_conflict(const DeviceId& requested,
                                 const DeviceId& existing,
                                 std::uint64_t owner) {
    throw std::runtime_error(
        "设备预留冲突: 请求 " + device_text(requested) +
        "，但现有预留 " + device_text(existing) +
        " 属于 cgroup " + std::to_string(owner));
}

}  // namespace

void validate_reservation_conflicts(
    std::uint64_t cgroup_id,
    const std::vector<DeviceId>& requested,
    const std::vector<std::pair<DeviceId, std::uint64_t>>& existing) {
    for (const DeviceId& request : requested) {
        for (const auto& reservation : existing) {
            const DeviceId& occupied = reservation.first;
            const std::uint64_t owner = reservation.second;
            if (owner == cgroup_id || occupied.major != request.major) {
                continue;
            }

            // major:* 与同 major 的任意精确或通配预留互斥；精确预留
            // 只与同一设备或覆盖它的 major:* 互斥。不同 minor 可以分别
            // 归属不同 sandbox。
            if (request.minor == kMinorWildcard ||
                occupied.minor == kMinorWildcard ||
                request.minor == occupied.minor) {
                throw_conflict(request, occupied, owner);
            }
        }
    }
}

}  // namespace neu_box::sandbox
