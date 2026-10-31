#include "executor.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>

#include "log.hpp"
#include "map_sync.hpp"
#include "planner.hpp"

namespace fms {

namespace {

double _DYNAMIC_REPLAN_RETRY = 1.0;
double _SENSOR_HYSTERESIS_MARGIN = 0.5;
double _STRAIGHT_SPEED = 0.2;  // m/s, STRAIGHT/DOCK/PARK 의 params.speed 기본값

// Liang-Barsky: 선분 a->b 가 사각형 [rx0,rx1]x[ry0,ry1] 과 겹치는가.
// planner.plan_path 와 같은 기준(사각형+robot_width/2 팽창)으로 막힘을 판정해야
// executor.py 에서 겪었던 "회피 경로 생성됐지만 계속 막힘 오판" 버그를 피한다.
bool segment_intersects_rect(double ax, double ay, double bx, double by, double rx0, double ry0,
                              double rx1, double ry1) {
    double dx = bx - ax, dy = by - ay;
    double t0 = 0.0, t1 = 1.0;
    double ps[4] = {-dx, dx, -dy, dy};
    double qs[4] = {ax - rx0, rx1 - ax, ay - ry0, ry1 - ay};
    for (int i = 0; i < 4; ++i) {
        double p = ps[i], q = qs[i];
        if (p == 0) {
            if (q < 0) return false;
            continue;
        }
        double t = q / p;
        if (p < 0) {
            if (t > t1) return false;
            t0 = std::max(t0, t);
        } else {
            if (t < t0) return false;
            t1 = std::min(t1, t);
        }
    }
    return t0 <= t1;
}

constexpr double kMaxTickDt = 0.1;  // s

}  // namespace

OrderExecutor::OrderExecutor(std::string robot_id_, double start_x, double start_y, double start_theta,
                              double max_velocity, double max_acceleration, BatterySim battery_,
                              const MapModel* map_model, double robot_width_, double robot_length_,
                              DynamicObstacleField obstacle_field_, double sensor_range_)
    : robot_id(std::move(robot_id_)), robot_width(robot_width_), robot_length(robot_length_),
      obstacle_field(std::move(obstacle_field_)), sensor_range(sensor_range_),
      physics(std::make_unique<PhysicsWorld>(RobotSpec{robot_width_, robot_length_}, start_x, start_y, start_theta)),
      kinematics(start_x, start_y, start_theta, max_velocity, max_acceleration, max_acceleration),
      battery(std::move(battery_)), map(map_model) {
    kinematics.attach_physics(physics.get());
    physics->set_map(map);
    if (map) chargers_ = load_chargers(map->base_dir);
}

void OrderExecutor::apply_robot_geometry(double friction, double mass) {
    RobotSpec spec = physics->spec();
    spec.width = robot_width;
    spec.length = robot_length;
    if (friction > 0) spec.friction = friction;
    if (mass > 0) spec.mass = mass;
    physics->set_spec(spec);
}

bool OrderExecutor::motion_inhibited() const {
    return estop_latched_ || paused_ || mode_ == OperatingMode::MANUAL || base_state_ == RobotState::ERROR ||
           localization_ != LocalizationState::LOCALIZED || (battery.charge <= 0.0 && !battery.charging);
}

bool OrderExecutor::can_accept_job() const {
    if (state() != RobotState::IDLE || mode_ != OperatingMode::AUTO || paused_ || !order_id_.empty() ||
        localization_ != LocalizationState::LOCALIZED || (battery.charge <= 0.0 && !battery.charging))
        return false;
    for (auto& e : errors_)
        if (e.level == ErrorLevel::FATAL) return false;
    return true;
}

std::string OrderExecutor::press_start() {
    if (estop_latched_) {
        log::warning("[%s] start ignored: release the e-stop latch first", robot_id.c_str());
        return "release the e-stop latch first";
    }
    if (base_state_ == RobotState::ERROR) {
        reset_error();
        return "error cleared";
    }
    if (paused_) {
        resume(PauseSource::LOCAL);
        return "pause released";
    }
    pause(PauseSource::LOCAL);
    return "paused";
}

std::string OrderExecutor::press_stop() {
    if (order_id_.empty()) {
        log::debug("[%s] stop button: no active order", robot_id.c_str());
        return "no active order";
    }
    log::info("[%s] stop button: cancelling order %s", robot_id.c_str(), order_id_.c_str());
    clear_order();
    return "order cancelled";
}

void OrderExecutor::set_estop(bool latched) {
    if (latched == estop_latched_) return;
    estop_latched_ = latched;
    if (latched) {
        kinematics.emergency_stop();
        log::warning("[%s] e-stop latched -- stopping immediately (job %s kept)", robot_id.c_str(), order_id_.c_str());
    } else {
        log::info("[%s] e-stop latch released -- returning to %s", robot_id.c_str(), to_string(base_state_).c_str());
        resume_motion();
    }
}

bool OrderExecutor::toggle_estop() {
    set_estop(!estop_latched_);
    return estop_latched_;
}

void OrderExecutor::set_mode(OperatingMode m) {
    if (m == mode_) return;
    mode_ = m;
    if (m == OperatingMode::MANUAL) {
        kinematics.emergency_stop();
        log::info("[%s] switched to MANUAL -- motion stopped (job %s kept)", robot_id.c_str(), order_id_.c_str());
    } else {
        log::info("[%s] switched to AUTO -- replanning from current pose", robot_id.c_str());
        resume_motion();
    }
}

bool OrderExecutor::pause(PauseSource source) {
    if (paused_) return false;
    paused_ = true;
    pause_source_ = source;
    kinematics.emergency_stop();
    log::info("[%s] paused (%s)", robot_id.c_str(), to_string(source).c_str());
    return true;
}

bool OrderExecutor::resume(PauseSource by) {
    if (!paused_) return false;
    if (by == PauseSource::FMS && pause_source_ == PauseSource::LOCAL) {
        log::warning("[%s] rejecting FMS resume: pause was set locally", robot_id.c_str());
        return false;
    }
    paused_ = false;
    pause_source_ = std::nullopt;
    log::info("[%s] pause released (%s)", robot_id.c_str(), to_string(by).c_str());
    resume_motion();
    return true;
}

void OrderExecutor::reset_error() {
    errors_.clear();
    if (base_state_ == RobotState::ERROR) {
        set_base(RobotState::IDLE);
        resume_motion();
    }
}

void OrderExecutor::resume_motion() {
    if (motion_inhibited()) return;
    if (acting_) return;
    replan();
}

void OrderExecutor::set_map(const MapModel* new_map, std::string map_uid, std::string map_version) {
    // 다른 맵(층)이면 좌표계가 다르다 -> 위치를 다시 잡아야 한다. 같은 맵의 버전만 바뀐 거면 유지
    const bool other_map = map_uid != current_map_uid_;
    map = new_map;
    physics->set_map(new_map);
    clearance_.reset();
    matcher_.reset();
    chargers_ = new_map ? load_chargers(new_map->base_dir) : std::vector<Charger>{};
    if (other_map) set_unlocalized("map changed");
    current_map_uid_ = std::move(map_uid);
    current_map_version_ = std::move(map_version);
    log::info("[%s] map switched: uid=%s version=%s", robot_id.c_str(), current_map_uid_.c_str(),
              current_map_version_.c_str());
    // 정지 지점이 노드 사이일 수 있고 새 맵에 금지구역이 추가됐을 수 있으므로
    // 현재 위치 기준으로 A* 를 다시 돌린다 (EMERGENCY/PAUSE/MANUAL 이면 재개 시 자동).
    resume_motion();
}

void OrderExecutor::set_action_status(const std::string& id, ActionStatus st) {
    for (auto& [aid, s] : action_states_)
        if (aid == id) { s = st; return; }
    action_states_.push_back({id, st});
}

ActionStatus OrderExecutor::get_action_status(const std::string& id, ActionStatus def) const {
    for (auto& [aid, s] : action_states_)
        if (aid == id) return s;
    return def;
}

bool OrderExecutor::accept_order(const Order& order) {
    if (last_completed_ && order.order_id == last_completed_->first &&
        order.order_update_id <= last_completed_->second) {
        log::info("[%s] ignoring already-completed order: %s update %d", robot_id.c_str(), order.order_id.c_str(),
                  order.order_update_id);
        return false;
    }

    if (order_id_ == order.order_id && !order_id_.empty()) {
        if (order.order_update_id <= order_update_id_) {
            log::info("[%s] ignoring stale order: %s update %d <= %d", robot_id.c_str(),
                      order.order_id.c_str(), order.order_update_id, order_update_id_);
            return false;
        }
        return apply_order_update(order);
    }

    if (!can_accept_job()) {
        if (charge_hold())
            log::warning("[%s] docked: battery %.1f%% < min_charge %.1f%% -- not accepting jobs yet", robot_id.c_str(),
                         battery.charge, docked_->min_charge);
        log::warning("[%s] rejecting order %s: state=%s mode=%s paused=%d active=%s localization=%s", robot_id.c_str(),
                     order.order_id.c_str(), to_string(state()).c_str(), to_string(mode_).c_str(),
                     paused_, order_id_.c_str(), to_string(localization_).c_str());
        return false;
    }

    if (battery.charge < battery_critical) {
        // 위험 수준: 충전하러 가는 오더(DOCK 포함)만 받는다
        bool to_charger = std::any_of(order.nodes.begin(), order.nodes.end(), [](const OrderNode& n) {
            return n.action && n.action->action_type == ActionType::DOCK;
        });
        if (!to_charger) {
            log::warning("[%s] rejecting order %s: battery %.1f%% < %.1f%% -- only DOCK orders accepted",
                         robot_id.c_str(), order.order_id.c_str(), battery.charge, battery_critical);
            return false;
        }
    }

    order_id_ = order.order_id;
    order_update_id_ = order.order_update_id;
    nodes_ = order.nodes;
    current_index_ = 0;
    action_states_.clear();
    for (auto& n : nodes_)
        if (n.action) set_action_status(n.action->action_id, ActionStatus::WAITING);
    acting_ = std::nullopt;

    int released_count = 0;
    for (auto& n : nodes_) if (n.released) ++released_count;
    log::info("[%s] order received %s update=%d, %zu nodes (%d released)", robot_id.c_str(),
              order.order_id.c_str(), order.order_update_id, nodes_.size(), released_count);
    replan();
    return true;
}

bool OrderExecutor::apply_order_update(const Order& order) {
    size_t old_len = nodes_.size();
    order_update_id_ = order.order_update_id;
    nodes_ = order.nodes;
    for (auto& n : nodes_) {
        if (!n.action) continue;
        bool known = std::any_of(action_states_.begin(), action_states_.end(),
                                  [&](auto& p) { return p.first == n.action->action_id; });
        if (!known) set_action_status(n.action->action_id, ActionStatus::WAITING);
    }

    if (!acting_) replan();

    int released_count = 0;
    for (auto& n : nodes_) if (n.released) ++released_count;
    log::info("[%s] order updated %s update=%d, nodes %zu -> %zu (%d released)", robot_id.c_str(),
              order.order_id.c_str(), order.order_update_id, old_len, nodes_.size(), released_count);
    return true;
}

void OrderExecutor::handle_instant(const InstantAction& action) {
    switch (action.action_type) {
        case InstantActionType::CANCEL_ORDER:
            log::info("[%s] order cancelled: %s", robot_id.c_str(), order_id_.c_str());
            clear_order();
            break;
        case InstantActionType::PAUSE:
            pause(PauseSource::FMS);
            break;
        case InstantActionType::RESUME:
            resume(PauseSource::FMS);
            break;
        case InstantActionType::RESET_ERROR:
            log::info("[%s] error reset", robot_id.c_str());
            reset_error();
            break;
        case InstantActionType::SET_MAP: {
            std::string uid = action.get_string("map_id", "");
            std::string version = action.get_string("map_version", "");
            std::string url = action.get_string("url", "");
            if (uid.empty() || version.empty() || url.empty()) {
                log::warning("[%s] SET_MAP ignored: map_id/map_version/url required", robot_id.c_str());
                break;
            }
            log::info("[%s] SET_MAP received: map_id=%s version=%s url=%s", robot_id.c_str(),
                      uid.c_str(), version.c_str(), url.c_str());
            if (map_sync)
                map_sync->request(uid, version, url);  // 백그라운드 다운로드, tick 루프가 회수
            else
                log::warning("[%s] SET_MAP: map sync disabled (no cache configured)", robot_id.c_str());
            break;
        }
        case InstantActionType::INJECT_FAULT: {
            std::string fault = action.get_string("fault", "UNKNOWN_FAULT");
            log::warning("[%s] fault injected: %s (order %s aborted)", robot_id.c_str(), fault.c_str(), order_id_.c_str());
            ErrorInfo e; e.error_type = fault; e.level = ErrorLevel::FATAL; e.description = "injected fault";
            raise_fatal(e);
            break;
        }
        case InstantActionType::INJECT_OBSTACLE: {
            std::string oid = action.has("obstacle_id") ? action.get_string("obstacle_id", "") : action.action_id;
            double x = action.get_double("x", 0.0), y = action.get_double("y", 0.0);
            double width = action.get_double("width", 0.4), height = action.get_double("height", 0.4);
            obstacle_field.add_manual(oid, x, y, width, height);
            log::info("[%s] manual obstacle placed: %s (%.2f, %.2f) %.2fx%.2f", robot_id.c_str(), oid.c_str(), x, y,
                      width, height);
            break;
        }
        case InstantActionType::CLEAR_OBSTACLE: {
            if (action.has("obstacle_id")) {
                std::string oid = action.get_string("obstacle_id", "");
                obstacle_field.clear(oid);
                log::info("[%s] manual obstacle removed: %s", robot_id.c_str(), oid.c_str());
            } else {
                obstacle_field.clear();
                log::info("[%s] manual obstacles removed: all", robot_id.c_str());
            }
            break;
        }
        case InstantActionType::START_OBSTACLE_ZONE: {
            Zone zone{action.get_double("x_min", 0), action.get_double("y_min", 0),
                      action.get_double("x_max", 0), action.get_double("y_max", 0)};
            std::optional<std::pair<ZoneSide, ZoneSide>> sides;
            std::string sides_str = action.get_string("sides", "");
            if (!sides_str.empty()) {
                auto dash = sides_str.find('-');
                sides = std::make_pair(zone_side_from_char(sides_str[0]),
                                        zone_side_from_char(sides_str[dash + 1]));
            }
            obstacle_field.configure_zone(zone, sides, action.get_double("spawn_interval", 5.0),
                                           action.get_int("max_count", 3), action.get_double("width", 0.4),
                                           action.get_double("height", 0.4), action.get_double("speed", 0.5));
            log::info("[%s] obstacle zone started: zone=(%.2f,%.2f,%.2f,%.2f)", robot_id.c_str(), zone.x_min,
                      zone.y_min, zone.x_max, zone.y_max);
            break;
        }
        case InstantActionType::STOP_OBSTACLE_ZONE:
            obstacle_field.stop_zone();
            log::info("[%s] obstacle zone spawning stopped (existing obstacles kept)", robot_id.c_str());
            break;
        case InstantActionType::INIT_POSITION: {
            if (!action.has("x") || !action.has("y")) {
                log::warning("[%s] INIT_POSITION ignored: x/y required", robot_id.c_str());
                break;
            }
            std::string uid = action.get_string("map_id", "");
            if (!uid.empty() && uid != current_map_uid_) {
                // 맵 교체(SET_MAP 다운로드)보다 먼저 온 힌트: 이 맵 좌표가 아니다
                log::warning("[%s] INIT_POSITION ignored: hint is for map %s but current map is %s", robot_id.c_str(),
                             uid.c_str(), current_map_uid_.empty() ? "(none)" : current_map_uid_.c_str());
                break;
            }
            localize_hint(action.get_double("x", 0.0), action.get_double("y", 0.0), action.get_double("theta", 0.0));
            break;
        }
    }
}

void OrderExecutor::tick(double dt) {
    // 경로계획 등으로 루프가 길게 막혔다 돌아오면 dt 가 크다 (A* 가 수백 ms 걸린다). 물리/궤적이 한 번에
    // 크게 움직이지 않도록 자른다. 막혀 있던 시간은 시뮬레이션에서 건너뛴다.
    dt = std::min(dt, kMaxTickDt);
    tick_logic(dt);
    sim_time_ += dt;
    if (recorder) record_sample(dt);
}

void OrderExecutor::record_sample(double dt) {
    RecordRow row;
    row.t = sim_time_;
    row.wall_time = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    row.state = to_string(state());
    if (sub_state_ && state() == RobotState::BUSY) row.sub_state = to_string(*sub_state_);
    row.order_id = order_id_;
    row.node_index = current_index_;
    if (straight_) row.action = "STRAIGHT_MOTION";
    if (acting_) row.action = to_string(acting_->action_type);
    row.w = physics->telemetry();
    row.battery = battery.charge;
    row.charging = battery.charging;
    row.detected_obstacles = (int)last_detected_.size();
    if (!errors_.empty()) row.error = errors_.front().error_type;
    recorder->write(row);
}

void OrderExecutor::tick_logic(double dt) {
    tick_localization(dt);
    check_battery();
    obstacle_field.tick(dt);
    auto detected = update_detections();
    last_detected_ = detected;

    if (motion_inhibited()) {
        battery.step(dt, 0.0);
        return;
    }
    if (straight_) {
        tick_straight(dt, detected);
        return;
    }
    if (acting_) {
        tick_action(dt);
        kinematics.brake(dt);
        battery.step(dt, 0.0);
        return;
    }
    if (order_id_.empty()) {
        set_base(idle_state());
        kinematics.brake(dt);
        battery.step(dt, 0.0);
        return;
    }
    if (current_index_ >= (int)nodes_.size()) {
        log::info("[%s] order complete: %s", robot_id.c_str(), order_id_.c_str());
        last_completed_ = std::make_pair(order_id_, order_update_id_);
        clear_order();
        kinematics.brake(dt);
        battery.step(dt, 0.0);
        return;
    }
    if (!nodes_[current_index_].released) {
        set_busy(BusySubState::WAITING);
        kinematics.brake(dt);
        battery.step(dt, 0.0);
        return;
    }
    if (segment_.empty()) {
        replan();
        if (segment_.empty()) {
            if (base_state_ != RobotState::ERROR) set_busy(BusySubState::WAITING);
            kinematics.brake(dt);
            battery.step(dt, 0.0);
            return;
        }
    }
    if (path_blocked_by(detected)) {
        auto kin_state = kinematics.hold(dt);
        battery.step(dt, kin_state.velocity);
        set_busy(BusySubState::WAITING);
        since_dynamic_replan_ += dt;
        if (since_dynamic_replan_ >= _DYNAMIC_REPLAN_RETRY) {
            since_dynamic_replan_ = 0.0;
            try_dynamic_replan(detected);
        }
        return;
    }
    since_dynamic_replan_ = 0.0;

    double prev_x = physics->x(), prev_y = physics->y();  // 충돌 롤백은 참 자세 기준
    auto kin_state = kinematics.step(dt);
    consume_reached_nodes();
    battery.step(dt, kin_state.velocity);

    if (check_collision(kin_state.x, kin_state.y, prev_x, prev_y)) return;

    if (kinematics.is_path_complete()) on_segment_arrived();
    else set_busy(BusySubState::MOVING);
}

bool OrderExecutor::check_collision(double, double, double prev_x, double prev_y) {
    // 벽과 닿았는지는 참 자세로 본다 (오도메트리 추정이 벽 안으로 가도 충돌이 아니다). 인자 x, y 는 쓰지 않는다
    const double x = physics->x(), y = physics->y();
    if (kinematics.collided()) {  // 물리: 몸체가 벽에 눌린 채 멈춰 있다
        double force = physics->wall_force();
        log::error("[%s] collision: chassis pressed against a wall at (%.2f, %.2f) (%.0f N), order %s aborted",
                   robot_id.c_str(), x, y, force, order_id_.c_str());
        ErrorInfo e; e.error_type = "COLLISION"; e.level = ErrorLevel::FATAL;
        char desc[96];
        std::snprintf(desc, sizeof desc, "chassis pressed against a wall at (%.2f, %.2f), contact force %.0f N", x, y, force);
        e.description = desc;
        raise_fatal(e);
        return true;
    }
    if (map == nullptr || map->is_free(x, y)) return false;
    CellType cell = map->cell_at(x, y);
    log::error("[%s] collision: (%.2f, %.2f), stopping at (%.2f, %.2f), order %s aborted", robot_id.c_str(), x, y, prev_x,
               prev_y, order_id_.c_str());
    kinematics.set_pose(prev_x, prev_y);
    ErrorInfo e; e.error_type = "COLLISION"; e.level = ErrorLevel::FATAL;
    e.description = "(" + std::to_string(x) + ", " + std::to_string(y) + ") is an " +
                     std::string(cell == CellType::OCCUPIED ? "OCCUPIED" : "UNKNOWN") + " cell";
    raise_fatal(e);
    return true;
}

std::vector<DynamicObstacle> OrderExecutor::update_detections() {
    double x = kinematics.x, y = kinematics.y;
    std::vector<DynamicObstacle> detected;
    for (auto& [id, obs] : obstacle_field.obstacles) {
        double threshold = sensor_range;
        if (detected_obstacle_ids_.count(id)) threshold += _SENSOR_HYSTERESIS_MARGIN;
        if (obs.distance_to(x, y) <= threshold) detected.push_back(obs);
    }
    std::set<std::string> now_ids;
    for (auto& o : detected) now_ids.insert(o.obstacle_id);
    for (auto& o : detected) {
        if (!detected_obstacle_ids_.count(o.obstacle_id))
            log::info("[%s] obstacle detected: %s (%.2f, %.2f) %.2fx%.2f source=%s", robot_id.c_str(),
                      o.obstacle_id.c_str(), o.x, o.y, o.width, o.height,
                      o.source == ObstacleSource::MANUAL ? "MANUAL" : "SIM");
    }
    for (auto& id : detected_obstacle_ids_)
        if (!now_ids.count(id)) log::info("[%s] obstacle no longer detected: %s", robot_id.c_str(), id.c_str());
    detected_obstacle_ids_ = now_ids;
    return detected;
}

bool OrderExecutor::path_blocked_by(const std::vector<DynamicObstacle>& detected) const {
    if (detected.empty()) return false;
    std::vector<std::pair<double, double>> points = {{kinematics.x, kinematics.y}};
    for (auto& p : kinematics.get_state().local_path) points.push_back(p);
    if (points.size() < 2) return false;
    double margin = robot_width / 2;
    for (auto& obs : detected) {
        double hw = obs.width / 2 + margin, hh = obs.height / 2 + margin;
        double rx0 = obs.x - hw, rx1 = obs.x + hw, ry0 = obs.y - hh, ry1 = obs.y + hh;
        for (size_t i = 0; i + 1 < points.size(); ++i) {
            if (segment_intersects_rect(points[i].first, points[i].second, points[i + 1].first,
                                         points[i + 1].second, rx0, ry0, rx1, ry1))
                return true;
        }
    }
    return false;
}

void OrderExecutor::try_dynamic_replan(const std::vector<DynamicObstacle>& detected) {
    if (map == nullptr || segment_.empty()) return;
    std::vector<std::tuple<double, double, double, double>> obstacles;
    for (auto& o : detected) obstacles.emplace_back(o.x, o.y, o.width, o.height);
    try {
        auto path = plan_segment_path(&obstacles);
        kinematics.set_path(path);
        kinematics.set_final_theta(nodes_[segment_.back()].position.theta);
        log::info("[%s] replanned around dynamic obstacle (%zu detected)", robot_id.c_str(), detected.size());
    } catch (const PlanningError& e) {
        log::debug("[%s] no detour around dynamic obstacle, keeping wait: %s", robot_id.c_str(), e.what());
    }
}

void OrderExecutor::raise_fatal(ErrorInfo error) {
    errors_.push_back(std::move(error));
    kinematics.emergency_stop();
    clear_order();
    set_base(RobotState::ERROR);
}

bool OrderExecutor::has_error(const char* type) const {
    return std::any_of(errors_.begin(), errors_.end(), [&](const ErrorInfo& e) { return e.error_type == type; });
}

void OrderExecutor::fail_action(const char* error_type, const std::string& desc) {
    if (acting_) set_action_status(acting_->action_id, ActionStatus::FAILED);
    log::error("[%s] %s: %s -- order %s aborted", robot_id.c_str(), error_type, desc.c_str(), order_id_.c_str());
    raise_fatal(ErrorInfo{error_type, ErrorLevel::FATAL, desc});
}

void OrderExecutor::check_battery() {
    const double c = battery.charge;
    const double prev = prev_charge_ < 0 ? c : prev_charge_;
    prev_charge_ = c;
    // 낮음: 알림만 (FMS 가 충전 보낼 근거). 2% 히스테리시스로 깜빡임 방지
    if (c < battery_low && !has_error("LOW_BATTERY")) {
        errors_.push_back(ErrorInfo{"LOW_BATTERY", ErrorLevel::WARNING, "battery below " + std::to_string((int)battery_low) + "%"});
        log::warning("[%s] LOW_BATTERY: %.1f%%", robot_id.c_str(), c);
    } else if (c >= battery_low + 2.0 && has_error("LOW_BATTERY")) {
        std::erase_if(errors_, [](const ErrorInfo& e) { return e.error_type == "LOW_BATTERY"; });
    }
    if (battery.charging) return;
    if (c <= 0.0) {
        if (!has_error("BATTERY_EMPTY")) {
            log::error("[%s] BATTERY_EMPTY: cannot drive (order %s aborted)", robot_id.c_str(), order_id_.c_str());
            raise_fatal(ErrorInfo{"BATTERY_EMPTY", ErrorLevel::FATAL, "battery empty: cannot drive"});
        }
        return;
    }
    // 위험: 내려가며 통과하는 순간 한 번만 (RESET_ERROR 뒤에는 DOCK 오더로 충전하러 갈 수 있게)
    if (prev >= battery_critical && c < battery_critical) {
        char desc[96];
        std::snprintf(desc, sizeof desc, "battery %.1f%% < %.1f%%: send to a charger (DOCK)", c, battery_critical);
        log::error("[%s] BATTERY_CRITICAL: %s (order %s aborted)", robot_id.c_str(), desc, order_id_.c_str());
        raise_fatal(ErrorInfo{"BATTERY_CRITICAL", ErrorLevel::FATAL, desc});
    }
}

void OrderExecutor::tick_action(double dt) {
    if (acting_->action_type == ActionType::CHARGE) set_base(RobotState::CHARGING);
    else set_busy(BusySubState::ACTING);

    acting_elapsed_ += dt;
    if (acting_elapsed_ < acting_->duration) return;
    set_action_status(acting_->action_id, ActionStatus::FINISHED);
    log::info("[%s] action complete: %s (%s)", robot_id.c_str(), acting_->action_id.c_str(),
              to_string(acting_->action_type).c_str());
    acting_ = std::nullopt;
    acting_elapsed_ = 0.0;
    battery.charging = docked_charging();
    replan();
}

void OrderExecutor::tick_straight(double dt, const std::vector<DynamicObstacle>& detected) {
    const StraightMotion m = *straight_;
    if (map && !map->occupancy_zones.empty()) {
        const std::vector<std::string> empty;
        const auto& allowed = current_index_ < (int)nodes_.size() ? nodes_[current_index_].allowed_zone_ids : empty;
        const double d = std::hypot(m.tx-kinematics.x, m.ty-kinematics.y);
        const int count = std::max(1, (int)std::ceil(d / 0.02));
        for (int i = 0; i <= count; ++i) {
            const double t = (double)i / count;
            if (map->traffic_blocked(kinematics.x+t*(m.tx-kinematics.x), kinematics.y+t*(m.ty-kinematics.y),
                                     std::hypot(robot_width, robot_length)/2+0.1, allowed)) {
                raise_fatal(ErrorInfo{"TRAFFIC_BLOCKED", ErrorLevel::FATAL, "직선 이동이 미허가 점유 존을 통과합니다"});
                kinematics.brake(dt);
                return;
            }
        }
    }
    if (!kinematics.straight_active()) kinematics.set_straight(m.tx, m.ty, m.speed);  // 시작, 또는 PAUSE 등에서 재개
    if (path_blocked_by(detected)) {  // 직선 구간은 우회하지 않는다. 비켜날 때까지 기다린다
        auto kin_state = kinematics.hold(dt);
        battery.step(dt, kin_state.velocity);
        set_busy(BusySubState::WAITING);
        return;
    }
    set_busy(m.purpose == StraightPurpose::UNDOCK ? BusySubState::MOVING : BusySubState::ACTING);
    double prev_x = physics->x(), prev_y = physics->y();  // 충돌 롤백은 참 자세 기준
    auto kin_state = kinematics.step(dt);
    battery.step(dt, kin_state.velocity);
    if (check_collision(kin_state.x, kin_state.y, prev_x, prev_y)) return;
    if (!kinematics.straight_active()) finish_straight();
}

void OrderExecutor::finish_straight() {
    const StraightMotion m = *straight_;
    straight_.reset();
    switch (m.purpose) {
        case StraightPurpose::ACTION:
        case StraightPurpose::DOCK_IN:
            if (m.purpose == StraightPurpose::DOCK_IN && docked_->charger && docked_->station) {
                // 접점은 실제 몸이 닿아야 한다: 참 자세로 본다 (추정 오차 + 도킹 오차가 허용 오차 안이어야)
                const Charger& c = *docked_->station;
                double off = std::hypot(physics->x() - c.x, physics->y() - c.y);
                double dth = std::abs(std::atan2(std::sin(physics->theta() - c.theta), std::cos(physics->theta() - c.theta)));
                bool side_ok = c.entry.empty() || c.entry == docked_->entry;
                if (off > c.tol_xy || dth > c.tol_theta || !side_ok) {
                    char desc[200];
                    if (!side_ok)
                        std::snprintf(desc, sizeof desc, "charger %s needs %s entry, robot entered %s", c.id.c_str(),
                                      c.entry.c_str(), docked_->entry.c_str());
                    else
                        std::snprintf(desc, sizeof desc, "no contact with charger %s: off %.3f m / %.3f rad (tol %.3f m / %.3f rad)",
                                      c.id.c_str(), off, dth, c.tol_xy, c.tol_theta);
                    fail_action("DOCK_FAILED", desc);  // docked_ 는 남는다: 다음 이동 때 먼저 빠져나온다
                    return;
                }
            }
            if (m.purpose == StraightPurpose::DOCK_IN) {
                docked_->contact = true;
                battery.charging = docked_->charger;
                if (docked_->charger && docked_->station)
                    log::info("[%s] docked on charger %s at (%.2f, %.2f): charging, jobs accepted from %.1f%% (now %.1f%%)",
                              robot_id.c_str(), docked_->station->id.c_str(), kinematics.x, kinematics.y,
                              docked_->min_charge, battery.charge);
                else if (docked_->charger)
                    log::info("[%s] docked at (%.2f, %.2f): charging, jobs accepted from %.1f%% (now %.1f%%)",
                              robot_id.c_str(), kinematics.x, kinematics.y, docked_->min_charge, battery.charge);
                else
                    log::info("[%s] parked at (%.2f, %.2f)", robot_id.c_str(), kinematics.x, kinematics.y);
            }
            if (acting_) {
                set_action_status(acting_->action_id, ActionStatus::FINISHED);
                log::info("[%s] action complete: %s (%s)", robot_id.c_str(), acting_->action_id.c_str(),
                          to_string(acting_->action_type).c_str());
            }
            acting_ = std::nullopt;
            acting_elapsed_ = 0.0;
            break;
        case StraightPurpose::UNDOCK:
            log::info("[%s] undocked to (%.2f, %.2f)", robot_id.c_str(), kinematics.x, kinematics.y);
            docked_.reset();
            break;
    }
    replan();
}

void OrderExecutor::start_undock() {
    Docked& d = *docked_;
    d.contact = false;
    battery.charging = false;
    straight_ = StraightMotion{d.exit_x, d.exit_y, d.speed, StraightPurpose::UNDOCK};
    kinematics.set_path({});
    set_busy(BusySubState::MOVING);
    log::info("[%s] undocking: straight to (%.2f, %.2f) before planning", robot_id.c_str(), d.exit_x, d.exit_y);
}

std::pair<double, double> OrderExecutor::approach_point(const OrderNode& node) const {
    // DOCK/PARK 노드 position 은 진입 완료 자세. FORWARD 진입이면 그 뒤쪽(heading 반대),
    // BACKWARD 진입이면 그 앞쪽에서 출발해야 heading 을 유지한 채 직선으로 들어간다.
    const Action& a = *node.action;
    double d = param_double(a.params, "undock_distance", 0.0);
    double sign = param_string(a.params, "entry", "FORWARD") == "FORWARD" ? -1.0 : 1.0;
    double th = node.position.theta;
    return {node.position.x + sign * d * std::cos(th), node.position.y + sign * d * std::sin(th)};
}

void OrderExecutor::replan() {
    if (straight_) return;  // 직선 이동 중 (끝나면 finish_straight 가 다시 부른다)
    segment_.clear();
    segment_wp_bounds_.clear();
    segment_wp_total_ = 0;
    if (motion_inhibited() || order_id_.empty()) {
        kinematics.set_path({});
        return;
    }
    for (int i = current_index_; i < (int)nodes_.size(); ++i) {
        auto& node = nodes_[i];
        if (!node.released) break;
        segment_.push_back(i);
        if (node.action) break;
    }
    if (segment_.empty()) {
        log::debug("[%s] skipping replan: no released nodes in this segment", robot_id.c_str());
        kinematics.set_path({});
        return;
    }
    if (docked_) {  // 도크/주차 칸 안은 A* 로 못 나온다. 진입했던 직선을 되짚어 먼저 빠져나온다
        segment_.clear();
        start_undock();
        return;
    }
    log::info("[%s] planning path from (%.2f, %.2f)", robot_id.c_str(), kinematics.x, kinematics.y);
    try {
        auto path = plan_segment_path();
        log::info("[%s] path planned: %zu waypoints", robot_id.c_str(), path.size());
        kinematics.set_path(path);
        kinematics.set_final_theta(nodes_[segment_.back()].position.theta);
    } catch (const PlanningError& e) {
        log::error("[%s] path planning failed: %s", robot_id.c_str(), e.what());
        segment_.clear();
        char at[64];
        std::snprintf(at, sizeof at, " [robot at (%.2f, %.2f)]", kinematics.x, kinematics.y);
        ErrorInfo err; err.error_type = "NO_PATH"; err.level = ErrorLevel::FATAL; err.description = std::string(e.what()) + at;
        raise_fatal(err);
    }
}

std::vector<std::pair<double, double>> OrderExecutor::plan_segment_path(
    const std::vector<std::tuple<double, double, double, double>>* dynamic_obstacles) {
    std::vector<std::pair<double, double>> targets;
    for (int i : segment_) {
        auto& n = nodes_[i];
        bool docking = n.action && (n.action->action_type == ActionType::DOCK || n.action->action_type == ActionType::PARK);
        targets.push_back(docking ? approach_point(n) : std::make_pair(n.position.x, n.position.y));
    }

    segment_wp_bounds_.clear();
    if (map == nullptr) {
        for (size_t k = 0; k < targets.size(); ++k) segment_wp_bounds_.push_back(k + 1);
        segment_wp_total_ = targets.size();
        return targets;
    }

    std::vector<std::pair<double, double>> full_path;
    std::pair<double, double> start = {kinematics.x, kinematics.y};
    for (auto& target : targets) {
        const auto& allowed = nodes_[segment_[segment_wp_bounds_.size()]].allowed_zone_ids;
        if ((centerline.enabled || path_refine.enabled) && !clearance_) clearance_ = std::make_unique<ClearanceField>(*map);
        // 복도 중앙 주행은 동적 장애물 회피 경로에도 쓴다 (장애물은 grid 에서 막히고, 거리 장은 벽 기준 선호만 준다).
        auto seg = plan_path(*map, start, target, robot_width, robot_length, dynamic_obstacles, &allowed,
                             clearance_.get(), &centerline);
        // 동적 장애물 회피 경로는 다듬지 않는다 (거리 장이 정적 맵 기준이라 장애물을 모름).
        if (path_refine.enabled && !dynamic_obstacles && seg.size() >= 2 && map->occupancy_zones.empty()) {
            seg = refine_path(*map, *clearance_, start, seg, robot_width, path_refine);
        }
        full_path.insert(full_path.end(), seg.begin(), seg.end());
        segment_wp_bounds_.push_back(full_path.size());
        start = target;
    }
    segment_wp_total_ = full_path.size();
    return full_path;
}

void OrderExecutor::consume_reached_nodes() {
    // 세그먼트 중간 노드를 물리적으로 지나갈 때마다 current_index_ 를 전진시킨다.
    // 이래야 EMERGENCY/PAUSE/MANUAL 로 멈췄다 재개할 때 replan 이 "다음 미도달
    // 노드" 부터 계획하고, 이미 지나온 경유지로 되돌아가지 않는다.
    // 세그먼트의 마지막 노드는 on_segment_arrived 가 처리한다 (action 실행 포함).
    if (segment_.empty() || segment_wp_bounds_.size() != segment_.size()) return;
    int remaining = (int)kinematics.get_state().local_path.size();
    int consumed = (int)segment_wp_total_ - remaining;
    if (consumed <= 0) return;

    int passed = 0;
    for (size_t k = 0; k + 1 < segment_.size(); ++k)
        if (consumed >= (int)segment_wp_bounds_[k]) passed = (int)k + 1;
    if (passed <= 0) return;

    int last_reached = segment_[passed - 1];
    if (last_reached + 1 <= current_index_) return;
    auto& node = nodes_[last_reached];
    last_node_id_ = node.node_id;
    last_node_sequence_id_ = node.sequence_id;
    current_index_ = last_reached + 1;
    log::info("[%s] node passed: %s (seq %d)", robot_id.c_str(), node.node_id.c_str(), node.sequence_id);
}

void OrderExecutor::on_segment_arrived() {
    if (segment_.empty()) { replan(); return; }
    int node_index = segment_.back();
    auto node = nodes_[node_index];
    segment_.clear();

    last_node_id_ = node.node_id;
    last_node_sequence_id_ = node.sequence_id;
    current_index_ = std::max(current_index_, node_index + 1);

    if (node.action && get_action_status(node.action->action_id, ActionStatus::WAITING) != ActionStatus::FINISHED) {
        acting_ = node.action;
        acting_elapsed_ = 0.0;
        set_action_status(node.action->action_id, ActionStatus::RUNNING);
        const Action& a = *node.action;
        double speed = param_double(a.params, "speed", _STRAIGHT_SPEED);
        if (a.action_type == ActionType::STRAIGHT) {
            // 노드 theta 방향으로 distance 만큼 (음수면 후진). 실제 heading 은 정렬 허용오차(~1도)만큼
            // 어긋나 있을 수 있지만 도착 오차는 시뮬레이션하지 않으므로 지령 방향 기준 목표점으로 간다.
            double d = param_double(a.params, "distance", 0.0);
            double th = node.position.theta;
            straight_ = StraightMotion{node.position.x + d * std::cos(th), node.position.y + d * std::sin(th), speed,
                                       StraightPurpose::ACTION};
            set_busy(BusySubState::ACTING);
            log::info("[%s] action started: %s (STRAIGHT %+.2fm)", robot_id.c_str(), a.action_id.c_str(), d);
            return;
        }
        if (a.action_type == ActionType::DOCK || a.action_type == ActionType::PARK) {
            bool charger = a.action_type == ActionType::DOCK;
            std::optional<Charger> station;
            if (charger) {
                // 충전기는 맵에 있는 물리 설비다 (chargers.json). 노드 근처에 없으면 들어가지 않는다
                const Charger* c = find_charger(chargers_, node.position.x, node.position.y, node.position.theta);
                if (c == nullptr) {
                    char desc[160];
                    std::snprintf(desc, sizeof desc, "no charger at (%.2f, %.2f, %.2f) (map has %zu)",
                                  node.position.x, node.position.y, node.position.theta, chargers_.size());
                    fail_action("DOCK_FAILED", desc);
                    return;
                }
                station = *c;
            }
            // 진입 직전 자세(= 지금 위치)가 언도킹 목표. 진입을 시작하는 순간부터 도킹 상태로 친다
            docked_ = Docked{charger, station, param_string(a.params, "entry", "FORWARD"), kinematics.x, kinematics.y,
                             charger ? param_double(a.params, "min_charge", 0.0) : 0.0, speed};
            straight_ = StraightMotion{node.position.x, node.position.y, speed, StraightPurpose::DOCK_IN};
            set_busy(BusySubState::ACTING);
            log::info("[%s] action started: %s (%s %s, %.2fm straight in)", robot_id.c_str(), a.action_id.c_str(),
                      to_string(a.action_type).c_str(), param_string(a.params, "entry", "").c_str(),
                      std::hypot(node.position.x - kinematics.x, node.position.y - kinematics.y));
            return;
        }
        if (node.action->action_type == ActionType::CHARGE) {
            // 제자리 충전은 충전기 접점에 붙어 있을 때만 된다 (DOCK 으로 들어간 뒤)
            if (!docked_charging()) {
                fail_action("CHARGE_FAILED", "not docked on a charger (DOCK first)");
                return;
            }
            set_base(RobotState::CHARGING);
            battery.charging = true;
        } else {
            set_busy(BusySubState::ACTING);
        }
        log::info("[%s] action started: %s (%s, %.1fs)", robot_id.c_str(), node.action->action_id.c_str(),
                  to_string(node.action->action_type).c_str(), node.action->duration);
        return;
    }

    replan();
    if (segment_.empty() && base_state_ != RobotState::ERROR) {
        if (current_index_ < (int)nodes_.size()) set_busy(BusySubState::WAITING);
        else set_base(RobotState::IDLE);
    }
}

void OrderExecutor::clear_order() {
    order_id_.clear();
    order_update_id_ = 0;
    nodes_.clear();
    current_index_ = 0;
    segment_.clear();
    segment_wp_bounds_.clear();
    segment_wp_total_ = 0;
    acting_ = std::nullopt;
    acting_elapsed_ = 0.0;
    action_states_.clear();
    straight_.reset();  // docked_ 는 남긴다 (다음 이동 때 먼저 언도킹)
    kinematics.set_path({});
    battery.charging = docked_charging();
    since_dynamic_replan_ = 0.0;
    set_base(idle_state());
}

void OrderExecutor::set_unlocalized(const char* why) {
    localization_ = LocalizationState::UNLOCALIZED;
    loc_score_ = 0.0;
    loc_hint_.reset();
    physics->set_tracking(false);
    kinematics.emergency_stop();
    log::warning("[%s] UNLOCALIZED (%s): send a pose hint (INIT_POSITION) before driving", robot_id.c_str(), why);
}

bool OrderExecutor::localize_hint(double x, double y, double theta) {
    if (map == nullptr) {
        log::warning("[%s] pose hint ignored: no map to match against (wait for SET_MAP or force localize)",
                     robot_id.c_str());
        return false;
    }
    if (!map->in_bounds(x, y)) {
        log::warning("[%s] pose hint ignored: (%.2f, %.2f) is outside the map", robot_id.c_str(), x, y);
        return false;
    }
    theta = std::atan2(std::sin(theta), std::cos(theta));
    kinematics.emergency_stop();
    physics->set_tracking(false);
    physics->set_estimate(x, y, theta);  // 로봇이 아는 자세 = 힌트 (매칭이 끝나면 고친다)
    kinematics.resync();
    localization_ = LocalizationState::LOCALIZING;
    loc_hint_ = Pose2{x, y, theta};
    loc_timer_ = localize_time;
    log::info("[%s] localizing around hint (%.2f, %.2f, %.2f)", robot_id.c_str(), x, y, theta);
    return true;
}

void OrderExecutor::tick_localization(double dt) {
    if (localization_ != LocalizationState::LOCALIZING) return;
    loc_timer_ -= dt;
    if (loc_timer_ > 0.0) return;
    if (map == nullptr || !loc_hint_) {
        set_unlocalized("map lost while localizing");
        return;
    }
    if (!matcher_) matcher_ = std::make_unique<ScanMatcher>(*map, 0.05, match_params.sigma_hit);
    // 가상 라이다는 참 자세에서 잰다. 매칭은 그 스캔만 보고 힌트 주변을 찾는다 (참 자세를 모른다)
    Pose2 truth{physics->x(), physics->y(), physics->theta()};
    auto scan = matcher_->simulate_scan(truth, lidar, lidar_rng);
    MatchResult r = matcher_->match(scan, *loc_hint_, match_params);
    loc_score_ = r.score;
    std::erase_if(errors_, [](const ErrorInfo& e) { return e.error_type == "LOCALIZATION_FAILED"; });
    if (r.ok) {
        physics->set_estimate(r.pose.x, r.pose.y, r.pose.theta);
        physics->set_tracking(true);
        kinematics.resync();
        localization_ = LocalizationState::LOCALIZED;
        loc_hint_.reset();
        log::info("[%s] LOCALIZED at (%.3f, %.3f, %.3f), score %.3f (%d scan points)", robot_id.c_str(), r.pose.x,
                  r.pose.y, r.pose.theta, r.score, r.points);
        resume_motion();
        return;
    }
    char desc[200];
    std::snprintf(desc, sizeof desc, "scan match failed near hint (%.2f, %.2f, %.2f): score %.2f < %.2f, %d points",
                  loc_hint_->x, loc_hint_->y, loc_hint_->theta, r.score, match_params.min_score, r.points);
    errors_.push_back(ErrorInfo{"LOCALIZATION_FAILED", ErrorLevel::WARNING, desc});
    localization_ = LocalizationState::UNLOCALIZED;
    loc_hint_.reset();
    log::warning("[%s] %s -- still UNLOCALIZED, send a better hint", robot_id.c_str(), desc);
}

bool OrderExecutor::force_relocalize(double x, double y, double theta) {
    docked_.reset();  // 다른 곳으로 옮겼으니 도킹은 풀린다
    clear_order();
    kinematics.emergency_stop();
    kinematics.set_pose(x, y, &theta);
    log::warning("[%s] force relocalized: (%.2f, %.2f, %.2f)", robot_id.c_str(), x, y, theta);
    // 벽/미탐색에서 주행 가능한 곳으로 빠져나왔으면 위치 때문에 난 오류(NO_PATH, COLLISION)를 푼다.
    // 다른 오류(배터리 등)는 그대로 둔다. 여전히 주행 불가 위치면 오류도 그대로다.
    if (map == nullptr || map->is_free(x, y)) {
        size_t n = std::erase_if(errors_, [](const ErrorInfo& e) {
            return e.error_type == "NO_PATH" || e.error_type == "COLLISION";
        });
        if (n) log::info("[%s] relocalized onto a free cell: cleared %zu position error(s)", robot_id.c_str(), n);
    } else {
        log::warning("[%s] relocalized position is not drivable: orders will fail with NO_PATH", robot_id.c_str());
    }
    // 참 자세를 옮겼고 추정도 그 자세에서 시작한다 (teleport 가 보정 오프셋을 지운다) -> 바로 위치를 안다
    std::erase_if(errors_, [](const ErrorInfo& e) { return e.error_type == "LOCALIZATION_FAILED"; });
    localization_ = LocalizationState::LOCALIZED;
    loc_score_ = 1.0;
    loc_hint_.reset();
    physics->set_tracking(true);
    return true;
}

bool OrderExecutor::sim_teleport(double x, double y, double theta, const std::string& map_uid) {
    if (!map_uid.empty() && map_uid != current_map_uid_) {
        log::warning("[%s] teleport ignored: for map %s but current map is %s", robot_id.c_str(), map_uid.c_str(),
                     current_map_uid_.empty() ? "(none)" : current_map_uid_.c_str());
        return false;
    }
    docked_.reset();
    clear_order();
    kinematics.emergency_stop();
    kinematics.set_pose(x, y, &theta);
    // 엘리베이터가 옮겼다: 몸은 거기 있지만 로봇은 모른다
    set_unlocalized("moved by elevator (teleport)");
    std::erase_if(errors_, [](const ErrorInfo& e) {
        return e.error_type == "NO_PATH" || e.error_type == "COLLISION" || e.error_type == "LOCALIZATION_FAILED";
    });
    log::warning("[%s] teleported: (%.2f, %.2f, %.2f)", robot_id.c_str(), x, y, theta);
    return true;
}

PhysicsDebug OrderExecutor::build_physics(long long header_id, double timestamp) const {
    PhysicsDebug d;
    d.header_id = header_id;
    d.timestamp = timestamp;
    d.robot_id = robot_id;
    d.sim_time = sim_time_;
    d.state = to_string(state());
    if (sub_state_ && state() == RobotState::BUSY) d.sub_state = to_string(*sub_state_);
    if (straight_) d.action = "STRAIGHT_MOTION";
    if (acting_) d.action = to_string(acting_->action_type);

    WorldTelemetry w = physics->telemetry();
    d.x = w.x; d.y = w.y; d.theta = w.theta;
    d.odom_x = w.odom_x; d.odom_y = w.odom_y; d.odom_theta = w.odom_theta;
    d.est_x = w.est_x; d.est_y = w.est_y; d.est_theta = w.est_theta;
    const LocalizerParams& lp = physics->localization();
    d.loc_rate = lp.rate_hz; d.loc_sigma_xy = lp.sigma_xy; d.loc_sigma_theta = lp.sigma_theta;
    d.loc_gain = lp.gain; d.loc_dropout = lp.dropout;
    d.loc_corrections = w.loc_corrections; d.loc_dropouts = w.loc_dropouts;
    d.loc_step_xy = w.loc_step_xy; d.loc_step_theta = w.loc_step_theta;
    d.v_fwd = w.v_fwd; d.v_lat = w.v_lat; d.yaw_rate = w.yaw_rate;
    for (int i = 0; i < 2; ++i) {
        d.wheel_w[i] = w.wheel_w[i];
        d.wheel_cmd[i] = w.wheel_cmd[i];
        d.slip[i] = w.slip[i];
    }
    d.cmd_v = w.cmd_v; d.cmd_w = w.cmd_w;
    d.wall_force = w.wall_force;
    d.ghost = w.ghost;
    d.collided = kinematics.collided();
    d.at_rest = physics->at_rest();
    d.straight = kinematics.straight_active();

    const RobotSpec& spec = physics->spec();
    d.friction = spec.friction;
    d.mass = spec.mass;
    d.wheel_radius = spec.wheel_radius;
    d.track = physics->track();
    d.max_wheel_torque = spec.max_wheel_torque;
    d.max_velocity = kinematics.max_velocity;
    d.max_acceleration = kinematics.max_acceleration;

    auto k = kinematics.get_state();
    d.path_points = (int)k.local_path.size();
    double px = w.x, py = w.y;
    for (auto& [x, y] : k.local_path) {
        d.path_remaining += std::hypot(x - px, y - py);
        px = x;
        py = y;
    }
    if (!k.local_path.empty()) {
        d.has_goal = true;
        d.goal_x = k.local_path.back().first;
        d.goal_y = k.local_path.back().second;
    }
    return d;
}

State OrderExecutor::build_state(long long header_id, double timestamp) const {
    auto k = kinematics.get_state();
    State s;
    s.header_id = header_id;
    s.timestamp = timestamp;
    s.robot_id = robot_id;
    s.state = state();
    s.sub_state = (s.state == RobotState::BUSY) ? sub_state_ : std::nullopt;
    s.mode = mode_;
    s.paused = paused_;
    s.pause_source = pause_source_;
    s.pose = Pose{std::round(k.x * 10000) / 10000, std::round(k.y * 10000) / 10000,
                  std::round(k.theta * 10000) / 10000};
    s.velocity = std::round(k.velocity * 10000) / 10000;
    s.width = robot_width;
    s.length = robot_length;
    for (auto& [x, y] : k.local_path)
        s.local_path.push_back(Point{std::round(x * 10000) / 10000, std::round(y * 10000) / 10000});
    if (!current_map_uid_.empty()) {
        s.map_id = current_map_uid_;
        s.map_version = current_map_version_;
    }
    s.localization = localization_;
    s.localization_score = std::round(loc_score_ * 1000) / 1000;
    s.last_node_id = last_node_id_;
    s.last_node_sequence_id = last_node_sequence_id_;
    s.order_id = order_id_.empty() ? std::nullopt : std::optional(order_id_);
    s.order_update_id = order_update_id_;
    for (int i = current_index_; i < (int)nodes_.size(); ++i) {
        auto& n = nodes_[i];
        s.node_states.push_back(NodeProgress{n.node_id, n.sequence_id, n.released});
    }
    for (auto& [aid, st] : action_states_) s.action_states.push_back(ActionProgress{aid, st});
    s.battery = battery.info();
    s.errors = errors_;
    for (auto& o : last_detected_)
        s.detected_obstacles.push_back(ObstacleInfo{std::round(o.x * 10000) / 10000,
                                                      std::round(o.y * 10000) / 10000, o.width, o.height});
    return s;
}

}  // namespace fms
