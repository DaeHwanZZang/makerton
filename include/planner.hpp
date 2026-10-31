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

class ClearanceField;

// 복도 중앙 주행. A* 칸 비용에 벽 근접 페널티를 더해 경로가 벽(팽창 경계)에 붙지 않고 통로 가운데로 가게 한다.
// 페널티 = weight * (1 - min(d, reach) / reach)^2, d = 가장 가까운 비-FREE 셀까지 거리(m).
// reach 보다 먼 곳은 페널티가 없어 넓은 홀에서는 직선으로 간다.
// 단순화는 시야(LOS)만 보지 않고 A* 경로에서 tolerance 이상 벗어나지 않는 범위에서만 경유점을 줄인다.
struct CenterlineParams {
    bool enabled = true;
    double weight = 6.0;
    double reach = 1.5;       // m
    double tolerance = 0.10;  // m
};

// dynamic_obstacles: world 좌표 사각형 (x,y 중심, width, height)
// clearance/centering 이 둘 다 있고 centering->enabled 면 복도 중앙 주행, 아니면 최단 경로 + LOS 단순화 (기존 동작)
std::vector<std::pair<double, double>> plan_path(
    const MapModel& map_model, std::pair<double, double> start, std::pair<double, double> goal,
    double robot_width = 0.5, double robot_length = 0.7,
    const std::vector<std::tuple<double, double, double, double>>* dynamic_obstacles = nullptr,
    const std::vector<std::string>* allowed_zone_ids = nullptr,
    const ClearanceField* clearance = nullptr, const CenterlineParams* centering = nullptr);

}  // namespace fms
