#include "dynamic_obstacles.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace fms {

ZoneSide zone_side_from_char(char c) {
    switch (c) {
        case '1': return ZoneSide::TOP;
        case '2': return ZoneSide::RIGHT;
        case '3': return ZoneSide::BOTTOM;
        case '4': return ZoneSide::LEFT;
    }
    throw std::runtime_error("side number must be 1-4");
}

double DynamicObstacle::distance_to(double px, double py) const {
    double hw = width / 2, hh = height / 2;
    double cx = std::max(x - hw, std::min(px, x + hw));
    double cy = std::max(y - hh, std::min(py, y + hh));
    return std::hypot(px - cx, py - cy);
}

bool DynamicObstacle::step(double dt) {
    if (!target) return false;
    double tx = target->first, ty = target->second;
    double dx = tx - x, dy = ty - y;
    double dist = std::hypot(dx, dy);
    if (dist < 1e-6) return true;
    double move = std::min(speed * dt, dist);
    x += dx / dist * move;
    y += dy / dist * move;
    return move >= dist - 1e-9;
}

void DynamicObstacleField::tick(double dt) {
    std::vector<std::string> arrived;
    for (auto& [id, obs] : obstacles)
        if (obs.step(dt)) arrived.push_back(id);
    for (auto& id : arrived) obstacles.erase(id);

    if (!has_zone_) return;
    since_spawn_ += dt;
    if (since_spawn_ < spawn_interval_) return;
    int sim_count = 0;
    for (auto& [id, o] : obstacles)
        if (o.source == ObstacleSource::SIM) ++sim_count;
    if (sim_count >= max_sim_count_) return;
    since_spawn_ = 0.0;
    spawn_crossing();
}

std::pair<double, double> DynamicObstacleField::edge_point(ZoneSide side, double along) const {
    double x_min = zone_.x_min, y_min = zone_.y_min, x_max = zone_.x_max, y_max = zone_.y_max;
    switch (side) {
        case ZoneSide::TOP: return {x_min + along * (x_max - x_min), y_min};
        case ZoneSide::BOTTOM: return {x_min + along * (x_max - x_min), y_max};
        case ZoneSide::LEFT: return {x_min, y_min + along * (y_max - y_min)};
        case ZoneSide::RIGHT: return {x_max, y_min + along * (y_max - y_min)};
    }
    return {x_min, y_min};
}

void DynamicObstacleField::spawn_crossing() {
    ZoneSide entry, exit_;
    if (sides_) {
        entry = sides_->first;
        exit_ = sides_->second;
    } else {
        std::vector<ZoneSide> all = {ZoneSide::TOP, ZoneSide::RIGHT, ZoneSide::BOTTOM, ZoneSide::LEFT};
        std::shuffle(all.begin(), all.end(), rng_);
        entry = all[0];
        exit_ = all[1];
    }
    std::uniform_real_distribution<double> along_dist(0.15, 0.85);
    auto [sx, sy] = edge_point(entry, along_dist(rng_));
    auto [tx, ty] = edge_point(exit_, along_dist(rng_));
    ++seq_;
    std::string oid = "SIM-" + std::to_string(seq_);
    DynamicObstacle obs;
    obs.obstacle_id = oid;
    obs.x = sx; obs.y = sy;
    obs.width = obstacle_width_; obs.height = obstacle_height_;
    obs.source = ObstacleSource::SIM;
    obs.target = std::make_pair(tx, ty);
    obs.speed = obstacle_speed_;
    obstacles[oid] = obs;
}

void DynamicObstacleField::configure_zone(Zone zone, std::optional<std::pair<ZoneSide, ZoneSide>> sides,
                                           double spawn_interval, int max_sim_count,
                                           double obstacle_width, double obstacle_height,
                                           double obstacle_speed) {
    zone_ = zone;
    has_zone_ = true;
    sides_ = sides;
    spawn_interval_ = spawn_interval;
    max_sim_count_ = max_sim_count;
    obstacle_width_ = obstacle_width;
    obstacle_height_ = obstacle_height;
    obstacle_speed_ = obstacle_speed;
    since_spawn_ = spawn_interval_;  // 다음 tick 에 바로 스폰
}

void DynamicObstacleField::add_manual(const std::string& id, double x, double y, double width,
                                       double height) {
    DynamicObstacle obs;
    obs.obstacle_id = id;
    obs.x = x; obs.y = y; obs.width = width; obs.height = height;
    obs.source = ObstacleSource::MANUAL;
    obstacles[id] = obs;
}

void DynamicObstacleField::clear(const std::optional<std::string>& id) {
    if (!id) obstacles.clear();
    else obstacles.erase(*id);
}

std::vector<DynamicObstacle> DynamicObstacleField::detected(double x, double y, double sensor_range) const {
    std::vector<DynamicObstacle> result;
    for (auto& [id, o] : obstacles)
        if (o.distance_to(x, y) <= sensor_range) result.push_back(o);
    return result;
}

}  // namespace fms
