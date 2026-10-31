#include "planner.hpp"

#include "path_refine.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <map>
#include <queue>
#include <unordered_map>
#include <unordered_set>

namespace fms {

namespace {

using Px = std::pair<int, int>;

struct PxHash {
    size_t operator()(const Px& p) const { return (size_t)p.first * 73856093u ^ (size_t)p.second * 19349663u; }
};

const std::vector<std::tuple<int, int, double>> kNeighbors = {
    {1, 0, 1.0}, {-1, 0, 1.0}, {0, 1, 1.0}, {0, -1, 1.0},
    {1, 1, 1.41421356}, {1, -1, 1.41421356}, {-1, 1, 1.41421356}, {-1, -1, 1.41421356},
};

// 정적 팽창 grid 캐시. 맵은 런타임에 안 바뀐다고 가정한다 (planner.py 의 _inflation_cache 와 동일 목적)
std::map<std::pair<const MapModel*, int>, std::vector<uint8_t>> g_inflation_cache;

std::vector<uint8_t> inflate(const std::vector<uint8_t>& grid, int w, int h, int radius_px) {
    if (radius_px <= 0) return grid;
    std::vector<bool> blocked(grid.size());
    for (size_t i = 0; i < grid.size(); ++i) blocked[i] = grid[i] != 2;
    std::vector<bool> result = blocked;

    for (int dy = -radius_px; dy <= radius_px; ++dy) {
        for (int dx = -radius_px; dx <= radius_px; ++dx) {
            if ((dx == 0 && dy == 0) || dx * dx + dy * dy > radius_px * radius_px) continue;
            int src_y0 = std::max(0, dy), src_y1 = h + std::min(0, dy);
            int src_x0 = std::max(0, dx), src_x1 = w + std::min(0, dx);
            int dst_y0 = std::max(0, -dy), dst_x0 = std::max(0, -dx);
            for (int sy = src_y0, ty = dst_y0; sy < src_y1; ++sy, ++ty) {
                for (int sx = src_x0, tx = dst_x0; sx < src_x1; ++sx, ++tx) {
                    if (blocked[(size_t)sy * w + sx]) result[(size_t)ty * w + tx] = true;
                }
            }
        }
    }
    std::vector<uint8_t> out = grid;
    for (size_t i = 0; i < out.size(); ++i)
        if (result[i]) out[i] = 0;
    return out;
}

const std::vector<uint8_t>& get_inflated_grid(const MapModel& map_model, int radius_px) {
    auto key = std::make_pair(&map_model, radius_px);
    auto it = g_inflation_cache.find(key);
    if (it != g_inflation_cache.end()) return it->second;
    const auto& grid = map_model.classify_grid();
    auto inflated = inflate(grid, map_model.metadata.width_px, map_model.metadata.height_px, radius_px);
    return g_inflation_cache.emplace(key, std::move(inflated)).first->second;
}

std::vector<uint8_t> apply_dynamic_obstacles(
    const MapModel& map_model, std::vector<uint8_t> grid, int w, int h,
    const std::vector<std::tuple<double, double, double, double>>& obstacles, double robot_width) {
    double margin = robot_width / 2;
    for (auto& [ox, oy, ow, oh] : obstacles) {
        auto [x0, y0] = map_model.world_to_pixel(ox - ow / 2 - margin, oy - oh / 2 - margin);
        auto [x1, y1] = map_model.world_to_pixel(ox + ow / 2 + margin, oy + oh / 2 + margin);
        x0 = std::max(0, std::min(x0, x1)); x1 = std::min(w - 1, std::max(x0, x1));
        y0 = std::max(0, std::min(y0, y1)); y1 = std::min(h - 1, std::max(y0, y1));
        if (x0 <= x1 && y0 <= y1) {
            for (int y = y0; y <= y1; ++y)
                for (int x = x0; x <= x1; ++x) grid[(size_t)y * w + x] = 0;
        }
    }
    return grid;
}

bool line_of_sight(Px a, Px b, const std::vector<uint8_t>& grid, int w, int h) {
    auto drivable = [&](int px, int py) {
        return px >= 0 && px < w && py >= 0 && py < h && grid[(size_t)py * w + px] == 2;
    };
    int x0 = a.first, y0 = a.second, x1 = b.first, y1 = b.second;
    int dx = std::abs(x1 - x0), dy = std::abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    int err = dx - dy;
    int x = x0, y = y0;
    while (x != x1 || y != y1) {
        if (!drivable(x, y)) return false;
        int e2 = 2 * err;
        if (e2 > -dy) { err -= dy; x += sx; }
        if (e2 < dx) { err += dx; y += sy; }
    }
    return drivable(x1, y1);
}

std::vector<Px> simplify(const std::vector<Px>& path_px, const std::vector<uint8_t>& grid, int w, int h) {
    if (path_px.size() <= 2) return path_px;
    std::vector<Px> result = {path_px[0]};
    size_t anchor = 0, i = 1;
    while (i < path_px.size() - 1) {
        if (!line_of_sight(path_px[anchor], path_px[i + 1], grid, w, h)) {
            result.push_back(path_px[i]);
            anchor = i;
        }
        ++i;
    }
    result.push_back(path_px.back());
    return result;
}

// 시야가 트이고, 사이의 A* 점이 모두 선분에서 tol_px 안에 있을 때만 건너뛴다.
// 복도 중앙을 따라 온 A* 경로를 벽 쪽으로 질러가지 않게 한다 (LOS 만 보면 팽창 경계를 스치는 지름길이 된다).
double point_segment_dist(Px p, Px a, Px b) {
    double vx = b.first - a.first, vy = b.second - a.second;
    double wx = p.first - a.first, wy = p.second - a.second;
    double len2 = vx * vx + vy * vy;
    double t = len2 > 0 ? std::clamp((wx * vx + wy * vy) / len2, 0.0, 1.0) : 0.0;
    return std::hypot(wx - t * vx, wy - t * vy);
}

std::vector<Px> simplify_within(const std::vector<Px>& path_px, const std::vector<uint8_t>& grid, int w, int h,
                                double tol_px) {
    if (path_px.size() <= 2) return path_px;
    std::vector<Px> result = {path_px[0]};
    size_t anchor = 0;
    while (anchor < path_px.size() - 1) {
        size_t best = anchor + 1;
        for (size_t j = anchor + 2; j < path_px.size(); ++j) {
            if (!line_of_sight(path_px[anchor], path_px[j], grid, w, h)) break;
            bool close = true;
            for (size_t k = anchor + 1; k < j && close; ++k)
                close = point_segment_dist(path_px[k], path_px[anchor], path_px[j]) <= tol_px;
            if (!close) break;
            best = j;
        }
        result.push_back(path_px[best]);
        anchor = best;
    }
    return result;
}

// cell_factor(px, py) >= 1: 그 칸에 들어가는 이동 비용 배수. nullptr 이면 1 (최단 경로).
// 배수가 1 이상이므로 유클리드 휴리스틱은 그대로 허용(admissible)이다.
std::optional<std::vector<Px>> astar(Px start, Px goal, const std::vector<uint8_t>& grid, int w, int h,
                                     const std::function<double(int, int)>* cell_factor = nullptr) {
    auto drivable = [&](int px, int py) {
        return px >= 0 && px < w && py >= 0 && py < h && grid[(size_t)py * w + px] == 2;
    };
    auto heuristic = [](Px a, Px b) { return std::hypot(a.first - b.first, a.second - b.second); };

    using Entry = std::pair<double, Px>;
    auto cmp = [](const Entry& a, const Entry& b) { return a.first > b.first; };
    std::priority_queue<Entry, std::vector<Entry>, decltype(cmp)> open(cmp);
    open.push({0.0, start});
    std::unordered_map<Px, Px, PxHash> came_from;
    std::unordered_map<Px, double, PxHash> g_score;
    g_score[start] = 0.0;
    std::unordered_set<Px, PxHash> visited;

    while (!open.empty()) {
        auto [_, current] = open.top();
        open.pop();
        if (visited.count(current)) continue;
        visited.insert(current);

        if (current == goal) {
            std::vector<Px> path = {current};
            while (came_from.count(current)) {
                current = came_from[current];
                path.push_back(current);
            }
            std::reverse(path.begin(), path.end());
            return path;
        }
        int cx = current.first, cy = current.second;
        for (auto& [dx, dy, cost] : kNeighbors) {
            int nx = cx + dx, ny = cy + dy;
            Px neighbor{nx, ny};
            if (visited.count(neighbor) || !drivable(nx, ny)) continue;
            if (dx != 0 && dy != 0 && !(drivable(cx + dx, cy) && drivable(cx, cy + dy))) continue;
            double tentative = g_score[current] + cost * (cell_factor ? (*cell_factor)(nx, ny) : 1.0);
            auto it = g_score.find(neighbor);
            if (it == g_score.end() || tentative < it->second) {
                came_from[neighbor] = current;
                g_score[neighbor] = tentative;
                open.push({tentative + heuristic(neighbor, goal), neighbor});
            }
        }
    }
    return std::nullopt;
}

}  // namespace

std::vector<std::pair<double, double>> plan_path(
    const MapModel& map_model, std::pair<double, double> start, std::pair<double, double> goal,
    double robot_width, double robot_length,
    const std::vector<std::tuple<double, double, double, double>>* dynamic_obstacles,
    const std::vector<std::string>* allowed_zone_ids,
    const ClearanceField* clearance, const CenterlineParams* centering) {
    const std::vector<std::string> empty;
    const auto& allowed = allowed_zone_ids ? *allowed_zone_ids : empty;
    const double traffic_margin = std::hypot(robot_width, robot_length) / 2 + 0.1;
    if (map_model.traffic_blocked(start.first, start.second, traffic_margin, allowed)
            || map_model.traffic_blocked(goal.first, goal.second, traffic_margin, allowed))
        throw PlanningError("endpoint intersects an unauthorized occupancy zone");

    if (!map_model.is_free(start.first, start.second))
        throw PlanningError("start point is not drivable");
    if (!map_model.is_free(goal.first, goal.second))
        throw PlanningError("goal point is not drivable");

    auto start_px = map_model.world_to_pixel(start.first, start.second);
    auto goal_px = map_model.world_to_pixel(goal.first, goal.second);
    if (start_px == goal_px) return {goal};

    int radius_px = (int)std::ceil((robot_width / 2) / map_model.metadata.resolution);
    std::vector<uint8_t> inflated = get_inflated_grid(map_model, radius_px);
    int w = map_model.metadata.width_px, h = map_model.metadata.height_px;
    if (dynamic_obstacles && !dynamic_obstacles->empty())
        inflated = apply_dynamic_obstacles(map_model, inflated, w, h, *dynamic_obstacles, robot_width);
    if (!map_model.occupancy_zones.empty()) {
        for (int y = 0; y < h; ++y) for (int x = 0; x < w; ++x) {
            const auto p = map_model.pixel_to_world(x, y);
            if (map_model.traffic_blocked(p.first, p.second, traffic_margin, allowed))
                inflated[(size_t)y*w+x] = 0;
        }
    }

    auto drivable = [&](int px, int py) {
        return px >= 0 && px < w && py >= 0 && py < h && inflated[(size_t)py * w + px] == 2;
    };
    if (!drivable(start_px.first, start_px.second) || !drivable(goal_px.first, goal_px.second)) {
        inflated[(size_t)start_px.second * w + start_px.first] = 2;
        inflated[(size_t)goal_px.second * w + goal_px.first] = 2;
    }

    const double res = map_model.metadata.resolution;
    const bool centered = clearance && centering && centering->enabled && centering->weight > 0
                          && centering->reach > 0;
    // 벽에서 먼 칸일수록 싸다. 복도에서는 가운데가 가장 싸고, reach 밖(넓은 홀)은 모두 같은 비용이다.
    const double reach_px = centered ? centering->reach / res : 0.0;
    std::function<double(int, int)> factor = [&](int px, int py) {
        double d = std::min(clearance->dist_at(px, py), reach_px);
        double gap = 1.0 - d / reach_px;
        return 1.0 + centering->weight * gap * gap;
    };
    auto path_px = astar(start_px, goal_px, inflated, w, h, centered ? &factor : nullptr);
    if (!path_px)
        throw PlanningError("no path found (blocked, or every corridor is narrower than the robot)");

    auto waypoints_px = centered ? simplify_within(*path_px, inflated, w, h, centering->tolerance / res)
                                 : simplify(*path_px, inflated, w, h);
    std::vector<std::pair<double, double>> waypoints;
    for (auto& p : waypoints_px) waypoints.push_back(map_model.pixel_to_world(p.first, p.second));
    if (!waypoints.empty()) waypoints.back() = goal;
    else waypoints = {goal};
    return waypoints;
}

}  // namespace fms
