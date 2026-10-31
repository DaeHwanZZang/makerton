// 로컬 경로계획 A* (robot_client/planner.py 포팅)
#pragma once

#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

#include "map_model.hpp"

namespace fms {

struct PlanningError : std::runtime_error {
    explicit PlanningError(const std::string& msg) : std::runtime_error(msg) {}
};

// dynamic_obstacles: world 좌표 사각형 (x,y 중심, width, height)
std::vector<std::pair<double, double>> plan_path(
    const MapModel& map_model, std::pair<double, double> start, std::pair<double, double> goal,
    double robot_width = 0.5, double robot_length = 0.7,
    const std::vector<std::tuple<double, double, double, double>>* dynamic_obstacles = nullptr);

}  // namespace fms
