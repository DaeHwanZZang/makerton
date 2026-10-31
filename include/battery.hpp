// 배터리 시뮬레이션 (robot_client/sim/battery.py 포팅)
#pragma once

#include <algorithm>
#include <cmath>
#include <optional>

#include "schemas.hpp"

namespace fms {

class BatterySim {
public:
    double charge;
    double idle_drain, move_drain, charge_rate, max_velocity;
    bool charging = false;

    explicit BatterySim(double charge_ = 100.0, double idle_drain_ = 0.01, double move_drain_ = 0.05,
                         double charge_rate_ = 1.0, double max_velocity_ = 1.2)
        : charge(std::max(0.0, std::min(100.0, charge_))), idle_drain(idle_drain_),
          move_drain(move_drain_), charge_rate(charge_rate_), max_velocity(max_velocity_) {}

    void step(double dt, double velocity = 0.0) {
        if (charging) {
            charge = std::min(100.0, charge + charge_rate * dt);
            last_drain_ = 0.0;
            return;
        }
        double speed_ratio = max_velocity > 0 ? std::min(1.0, std::abs(velocity) / max_velocity) : 0.0;
        double drain = idle_drain + move_drain * speed_ratio;
        last_drain_ = drain;
        charge = std::max(0.0, charge - drain * dt);
    }

    std::optional<double> estimated_runtime() const {
        if (charging || last_drain_ <= 0) return std::nullopt;
        return charge / last_drain_;
    }

    BatteryInfo info() const {
        BatteryInfo b;
        b.charge = std::round(charge * 100) / 100.0;
        b.charging = charging;
        b.estimated_runtime = estimated_runtime();
        return b;
    }

private:
    double last_drain_ = idle_drain;
};

}  // namespace fms
