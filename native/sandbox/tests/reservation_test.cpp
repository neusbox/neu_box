#include "sandbox.hpp"

#include <cstdint>
#include <exception>
#include <iostream>
#include <string_view>
#include <utility>
#include <vector>

namespace {

using neu_box::sandbox::DeviceId;
using neu_box::sandbox::kMinorWildcard;
using Reservation = std::pair<DeviceId, std::uint64_t>;

bool expect_allowed(std::string_view name,
                    std::uint64_t owner,
                    const std::vector<DeviceId>& requested,
                    const std::vector<Reservation>& existing) {
    try {
        neu_box::sandbox::validate_reservation_conflicts(
            owner, requested, existing);
        return true;
    } catch (const std::exception& error) {
        std::cerr << name << ": expected allowed, got: " << error.what()
                  << '\n';
        return false;
    }
}

bool expect_conflict(std::string_view name,
                     std::uint64_t owner,
                     const std::vector<DeviceId>& requested,
                     const std::vector<Reservation>& existing) {
    try {
        neu_box::sandbox::validate_reservation_conflicts(
            owner, requested, existing);
    } catch (const std::exception&) {
        return true;
    }
    std::cerr << name << ": expected conflict, got allowed\n";
    return false;
}

}  // namespace

int main() {
    constexpr std::uint64_t owner_a = 1001;
    constexpr std::uint64_t owner_b = 2002;
    const DeviceId device_0{234, 0};
    const DeviceId device_1{234, 1};
    const DeviceId wildcard{234, kMinorWildcard};

    bool passed = true;
    passed &= expect_conflict(
        "exact cannot steal exact", owner_b, {device_0},
        {{device_0, owner_a}});
    passed &= expect_allowed(
        "different exact minors may have different owners", owner_b,
        {device_1}, {{device_0, owner_a}});
    passed &= expect_conflict(
        "exact cannot overlap another owner's wildcard", owner_b,
        {device_0}, {{wildcard, owner_a}});
    passed &= expect_conflict(
        "wildcard cannot overlap another owner's exact", owner_b,
        {wildcard}, {{device_0, owner_a}});
    passed &= expect_conflict(
        "wildcard cannot steal wildcard", owner_b, {wildcard},
        {{wildcard, owner_a}});
    passed &= expect_allowed(
        "same owner may repeat or widen its reservation", owner_a,
        {device_0, wildcard}, {{device_0, owner_a}, {wildcard, owner_a}});
    passed &= expect_allowed(
        "different majors do not conflict", owner_b, {{195, 0}},
        {{device_0, owner_a}, {wildcard, owner_a}});

    return passed ? 0 : 1;
}
