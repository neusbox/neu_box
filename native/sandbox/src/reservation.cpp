#include "sandbox.hpp"

#include <stdexcept>
#include <string>

namespace neu_box::sandbox {
namespace {

auto device_text(const DeviceId& device) -> std::string {
    return std::to_string(device.major) + ':' + std::to_string(device.minor);
}

[[noreturn]]
void throw_conflict(const DeviceId& requested, const DeviceId& existing, std::uint64_t owner) {
    throw std::runtime_error("设备预留冲突: 请求 " + device_text(requested) + "，但现有预留 " +
                             device_text(existing) + " 属于 cgroup " + std::to_string(owner));
}

} // namespace

void validate_reservation_conflicts(
    std::uint64_t cgroup_id, const std::vector<DeviceId>& requested,
    const std::vector<std::pair<DeviceId, std::uint64_t>>& existing) {
    for (const DeviceId& request : requested) {
        for (const auto& reservation : existing) {
            const DeviceId& occupied = reservation.first;
            const std::uint64_t owner = reservation.second;
            if (owner != cgroup_id && occupied.major == request.major &&
                occupied.minor == request.minor) {
                throw_conflict(request, occupied, owner);
            }
        }
    }
}

} // namespace neu_box::sandbox
