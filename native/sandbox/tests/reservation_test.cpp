#include "sandbox.hpp"

#include <cstdint>
#include <exception>
#include <iostream>
#include <string_view>
#include <utility>
#include <vector>

namespace {

using neu_box::sandbox::DeviceId;
using Reservation = std::pair<DeviceId, std::uint64_t>;

auto expect_allowed(std::string_view name, std::uint64_t owner,
                    const std::vector<DeviceId>& requested,
                    const std::vector<Reservation>& existing) -> bool {
    try {
        neu_box::sandbox::validate_reservation_conflicts(owner, requested, existing);
        return true;
    } catch (const std::exception& error) {
        std::cerr << name << ": expected allowed, got: " << error.what() << '\n';
        return false;
    }
}

auto expect_conflict(std::string_view name, std::uint64_t owner,
                     const std::vector<DeviceId>& requested,
                     const std::vector<Reservation>& existing) -> bool {
    try {
        neu_box::sandbox::validate_reservation_conflicts(owner, requested, existing);
    } catch (const std::exception&) {
        return true;
    }
    std::cerr << name << ": expected conflict, got allowed\n";
    return false;
}

} // namespace

auto main() -> int {
    constexpr std::uint64_t owner_a = 1001;
    constexpr std::uint64_t owner_b = 2002;
    const DeviceId device_0{234, 0};
    const DeviceId device_1{234, 1};

    bool passed = true;
    passed &=
        expect_conflict("exact cannot steal exact", owner_b, {device_0}, {{device_0, owner_a}});
    passed &= expect_allowed("different exact minors may have different owners", owner_b,
                             {device_1}, {{device_0, owner_a}});
    passed &= expect_allowed("same owner may repeat its reservation", owner_a, {device_0},
                             {{device_0, owner_a}});
    passed &= expect_allowed("different majors do not conflict", owner_b, {{195, 0}},
                             {{device_0, owner_a}});

    return passed ? 0 : 1;
}
