#include "kinematics.hpp"

#include <algorithm>
#include <cmath>

#include "log.hpp"
#include "physics_world.hpp"

namespace fms {

namespace {

constexpr double kPi = 3.14159265358979323846;
// 이 각도 오차 이내면 "정렬 완료"로 본다. 사다리꼴 각가속도 프로파일이 정확히
// 0으로 수렴하지 못하는 부동소수 오차를 흡수하기 위한 여유.
constexpr double kHeadingTolerance = 0.02;  // rad (~1.1도)

// 물리 모드. 이상 궤적은 끝났는데 실제 로봇이 목표에서 이보다 멀면 다시 목표를 건다.
constexpr double kArriveTolerance = 0.03;     // m, 경로 마지막 지점
// 물리 모드의 경로 주행 방위 허용치. 이상 궤적(0.02 rad)대로 하면 슬립/지연 때문에 서고-돌고를 반복한다.
constexpr double kStartHeadingTolerance = 0.05;  // rad, 서 있다가 출발할 때
constexpr double kMovingHeadingTolerance = 0.30; // rad, 달리는 중에는 이 안이면 멈추지 않고 조향하며 간다
constexpr double kMovingSpeed = 0.05;            // m/s, "달리는 중" 기준

// 물리 모드: 이상 궤적의 속도가 실제 속도에서 이만큼 이상 앞서/뒤처지지 못하게 묶는다.
// 슬립으로 실제가 못 따라가는데 이상 궤적만 달아나면 감속 시점이 어긋나 목표를 지나친다.
constexpr double kVelocityBand = 0.3;            // m/s
constexpr double kAngularBand = 0.5;             // rad/s

constexpr double kWaypointReach = 0.05;       // m, 물리 모드: 이 안의 경유점은 이미 닿은 것으로 보고 건너뛴다 (마지막 점 제외)
constexpr double kAimMinDist = 0.02;          // m, 물리 모드: 목표 경유점이 이보다 가까우면 방위를 목표로 돌리지 않고 유지한다 (도착 허용 3 cm 보다 작아야 마지막 보정 접근이 된다)
constexpr double kStraightAlong = 0.01;       // m, 직선 이동 도착: 진행 방향 남은 거리 (옆 방향 오차는 직선으로 못 고친다)
constexpr double kStraightAimDist = 0.3;      // m, 목표가 이보다 멀면 목표 방위로 조향하고 가까우면 heading 을 유지한다
constexpr double kStraightHeadingGain = 3.0;  // 직선 이동 중 방위 유지 P 게인 (1/s)
constexpr double kSettleSpeed = 0.1;          // m/s, 정착 중 마무리 접근 속도
constexpr double kSettleTimeout = 4.0;        // s, 이 시간 안에 못 서면 그냥 도착으로 본다

double normalize_angle(double a) {
    while (a > kPi) a -= 2 * kPi;
    while (a < -kPi) a += 2 * kPi;
    return a;
}

}  // namespace

double TrapezoidalKinematics::remaining_length() const {
    if (remaining_.empty()) return 0.0;
    double total = std::hypot(remaining_[0].first - x, remaining_[0].second - y);
    for (size_t i = 0; i + 1 < remaining_.size(); ++i) {
        total += std::hypot(remaining_[i + 1].first - remaining_[i].first,
                             remaining_[i + 1].second - remaining_[i].second);
    }
    return total;
}

double TrapezoidalKinematics::stop_distance_ahead() const {
    // 물리 모드: 로봇은 달리던 속도로 모퉁이를 순간적으로 돌 수 없다. 방향이 크게 꺾이는 경유점에서 서도록
    // 거기까지의 거리를 감속 기준으로 쓴다 (꺾임이 작으면 서지 않고 조향하며 지나간다). 없으면 경로 끝까지.
    if (remaining_.empty()) return 0.0;
    double total = std::hypot(remaining_[0].first - x, remaining_[0].second - y);
    double prev_dir = std::atan2(remaining_[0].second - y, remaining_[0].first - x);
    for (size_t i = 0; i + 1 < remaining_.size(); ++i) {
        double dx = remaining_[i + 1].first - remaining_[i].first, dy = remaining_[i + 1].second - remaining_[i].second;
        double len = std::hypot(dx, dy);
        if (len < 1e-9) continue;
        double dir = std::atan2(dy, dx);
        if (std::abs(normalize_angle(dir - prev_dir)) > kMovingHeadingTolerance) return total;
        total += len;
        prev_dir = dir;
    }
    return total;
}

void TrapezoidalKinematics::advance(double dist) {
    // theta 는 더이상 여기서 건드리지 않는다 -- rotate_step 이 사다리꼴 각속도로 관리한다.
    while (dist > 1e-9 && !remaining_.empty()) {
        double tx = remaining_[0].first, ty = remaining_[0].second;
        double dx = tx - x, dy = ty - y;
        double seg_len = std::hypot(dx, dy);
        if (seg_len < 1e-9) { remaining_.erase(remaining_.begin()); continue; }
        if (dist >= seg_len) {
            x = tx; y = ty;
            dist -= seg_len;
            remaining_.erase(remaining_.begin());
        } else {
            double frac = dist / seg_len;
            x += dx * frac;
            y += dy * frac;
            dist = 0.0;
        }
    }
}

double TrapezoidalKinematics::rotate_step(double target_theta, double dt) {
    double error = normalize_angle(target_theta - theta);
    double abs_error = std::abs(error);
    if (abs_error < 1e-9) { angular_velocity_ = 0.0; return 0.0; }
    double sign = error > 0 ? 1.0 : -1.0;
    double speed = std::abs(angular_velocity_);
    double stop_angle = max_angular_acceleration > 0 ? (speed * speed) / (2 * max_angular_acceleration) : 0.0; //지금 속도에서 최대로 감속했을 때 멈출 때까지 도는 각도
    double accel_mag;
    if (abs_error <= stop_angle) accel_mag = -max_angular_acceleration;
    else if (speed < max_angular_velocity) accel_mag = max_angular_acceleration;
    else accel_mag = 0.0;
    double new_speed = std::max(0.0, std::min(max_angular_velocity, speed + accel_mag * dt));

    double turn = std::min((speed + new_speed) / 2 * dt, abs_error); //이번 틱 회전량

    theta = normalize_angle(theta + sign * turn);
    angular_velocity_ = sign * new_speed;
    return normalize_angle(target_theta - theta);
}

RobotKinematicState TrapezoidalKinematics::hold_ideal(double dt) {
    velocity_ = std::max(0.0, velocity_ - max_deceleration * dt);
    acceleration_ = velocity_ > 1e-9 ? -max_deceleration : 0.0;
    double speed = std::max(0.0, std::abs(angular_velocity_) - max_angular_acceleration * dt);
    angular_velocity_ = angular_velocity_ >= 0 ? speed : -speed;
    return get_state();
}

RobotKinematicState TrapezoidalKinematics::step_straight(double dt) {
    // 회전 없이 목표점까지 사다리꼴 속도로 직선 이동. velocity_ 는 크기만 갖는다 (후진도 양수).
    angular_velocity_ = 0.0;
    double dx = straight_->tx - x, dy = straight_->ty - y;
    double dist = std::hypot(dx, dy);
    double vmax = std::min(max_velocity, straight_->max_speed);
    double stop_dist = max_deceleration > 0 ? (velocity_ * velocity_) / (2 * max_deceleration) : 0.0;
    double accel;
    if (dist <= stop_dist) accel = -max_deceleration;
    else if (velocity_ < vmax) accel = max_acceleration;
    else accel = 0.0;
    double new_velocity = std::max(0.0, std::min(vmax, velocity_ + accel * dt));
    // 감속 끝에 속도가 0 이 되어 목표 직전에서 멈추는 일을 막는다 (최소 속도로 마저 들어간다)
    double move = std::max((velocity_ + new_velocity) / 2 * dt, std::min(dist, 0.01 * dt));
    if (move >= dist - 1e-9) {
        x = straight_->tx;
        y = straight_->ty;
        straight_.reset();
        velocity_ = 0.0;
        acceleration_ = 0.0;
        return get_state();
    }
    x += dx / dist * move;
    y += dy / dist * move;
    velocity_ = new_velocity;
    acceleration_ = accel;
    return get_state();
}

RobotKinematicState TrapezoidalKinematics::step_ideal(double dt) {
    if (straight_) return step_straight(dt);
    // 위치추정에 노이즈가 있으면 가까운 경유점으로의 방위(atan2)가 마구 흔들려 제자리에서 헌팅한다
    // (구간마다 시작점이 이전 구간 끝과 겹치는 경유점이 특히 그렇다). 이미 닿은 경유점은 건너뛴다.
    if (world_) {
        while (remaining_.size() > 1 &&
               std::hypot(remaining_[0].first - x, remaining_[0].second - y) < kWaypointReach)
            remaining_.erase(remaining_.begin());
    }
    double remaining_dist = remaining_length();
    bool has_target = false;
    double heading_error = 0.0;


    //경로가 남았을 때 목적지까지의 방향으로 회전
    if (remaining_dist > 1e-9) {
        double dx = remaining_[0].first - x, dy = remaining_[0].second - y;
        bool too_close = world_ && std::hypot(dx, dy) < kAimMinDist;  // 방위가 노이즈에 좌우된다 -> 지금 방위 유지
        heading_error = rotate_step(too_close ? theta : std::atan2(dy, dx), dt);
        has_target = true;
    //경로가 남지 않았을 때 최종각도까지 회전
    } else if (final_theta_) {
        heading_error = rotate_step(*final_theta_, dt);
        has_target = true;
    } else {
        angular_velocity_ = 0.0;
    }

    // 목표 방향으로 정렬되기 전까지는 직진하지 않는다 (제자리 회전 후 전진).
    double facing_tol = kHeadingTolerance;
    if (world_ && remaining_dist > 1e-9) facing_tol = velocity_ > kMovingSpeed ? kMovingHeadingTolerance : kStartHeadingTolerance;
    bool facing = !has_target || std::abs(heading_error) < facing_tol;

    double accel;
    double stop_limit = remaining_dist;  // 이번 tick 에 이 거리 이상은 가지 않는다 (모퉁이에서 서기 위해)
    if (!facing || remaining_dist < 1e-9) {
        accel = velocity_ > 1e-9 ? -max_deceleration : 0.0;
    } else {
        double stop_dist = max_deceleration > 0 ? (velocity_ * velocity_) / (2 * max_deceleration) : 0.0;
        // 물리 모드는 모퉁이(큰 꺾임)에서 서도록 거기까지를 감속 기준으로 쓴다
        double brake_ahead = world_ ? stop_distance_ahead() : remaining_dist;
        if (brake_ahead <= stop_dist) accel = -max_deceleration;
        else if (velocity_ < max_velocity) accel = max_acceleration;
        else accel = 0.0;
        stop_limit = brake_ahead;
    }
    double new_velocity = std::max(0.0, std::min(max_velocity, velocity_ + accel * dt));
    double move_dist = facing ? std::min({(velocity_ + new_velocity) / 2 * dt, remaining_dist, stop_limit}) : 0.0;
    advance(move_dist);

    velocity_ = remaining_length() > 1e-9 ? new_velocity : 0.0;
    acceleration_ = accel;
    return get_state();
}

bool TrapezoidalKinematics::is_path_complete() const {
    if (straight_ || settle_) return false;
    if (remaining_length() >= 1e-9) return false;
    if (final_theta_ && std::abs(normalize_angle(*final_theta_ - theta)) >= kHeadingTolerance) return false;
    if (world_ && !world_->at_rest()) return false;  // 물리 모드: 실제로 멈춰야 도착
    return true;
}

RobotKinematicState TrapezoidalKinematics::get_state() const {
    // 직선 이동 중에는 목표점 하나를 local_path 로 보고한다 (뷰어 표시, 동적 장애물 막힘 판정용)
    std::vector<std::pair<double, double>> path = remaining_;
    if (straight_) path = {{straight_goal_.tx, straight_goal_.ty}};
    else if (settle_) path = {{settle_->tx, settle_->ty}};
    double v = velocity_, w = angular_velocity_;
    if (world_) {  // 물리 모드는 실제 속도를 보고한다 (속도는 크기만, 후진도 양수)
        v = std::abs(world_->v_fwd());
        w = world_->yaw_rate();
    }
    return RobotKinematicState{x, y, theta, v, acceleration_, w, std::move(path), is_path_complete()};
}

void TrapezoidalKinematics::set_straight(double tx, double ty, double max_speed) {
    if (world_) sync_from_world();
    remaining_.clear();
    goal_.reset();
    final_theta_.reset();
    settle_.reset();
    straight_ = Straight{tx, ty, max_speed};
    straight_goal_ = *straight_;
    straight_theta_ = theta;
    straight_reverse_ = (tx - x) * std::cos(theta) + (ty - y) * std::sin(theta) < 0;
}

void TrapezoidalKinematics::set_pose(double nx, double ny, const double* ntheta) {
    x = nx;
    y = ny;
    if (ntheta) theta = *ntheta;
    if (world_) world_->teleport(x, y, theta);
}

void TrapezoidalKinematics::emergency_stop() {
    velocity_ = 0.0; acceleration_ = 0.0; angular_velocity_ = 0.0;
    remaining_.clear(); final_theta_.reset(); straight_.reset(); goal_.reset(); settle_.reset();
    cmd_v_ = cmd_w_ = 0.0;
    if (world_) world_->stop();
}

void TrapezoidalKinematics::attach_physics(PhysicsWorld* world) {
    world_ = world;
    if (world_) sync_from_world();
}

bool TrapezoidalKinematics::collided() const { return world_ && world_->collided(); }

void TrapezoidalKinematics::sync_from_world() {
    // 로봇이 아는 자세 = 오도메트리 + 위치추정 보정(꺼져 있으면 순수 오도메트리). 참 자세(world_->x() 등)는 로봇이 모른다.
    // 제어/경로추종/도착판정/State.pose 전부 이 값으로 돈다.
    x = world_->est_x();
    y = world_->est_y();
    theta = world_->est_theta();
}

void TrapezoidalKinematics::brake(double dt) {
    if (!world_ || world_->at_rest()) return;
    hold_physical(dt);
}

RobotKinematicState TrapezoidalKinematics::step(double dt) {
    if (!world_) return step_ideal(dt);
    return step_physical(dt);
}

RobotKinematicState TrapezoidalKinematics::hold(double dt) {
    if (!world_) return hold_ideal(dt);
    return hold_physical(dt);
}

RobotKinematicState TrapezoidalKinematics::hold_physical(double dt) {
    // 현재 속도에서 max_deceleration 으로 감속시키며 선다 (E-STOP 처럼 즉시 서지는 않는다)
    sync_from_world();
    double v = world_->v_fwd(), w = world_->yaw_rate();
    double dv = max_deceleration * dt, dw = max_angular_acceleration * dt;
    cmd_v_ = v > 0 ? std::max(0.0, v - dv) : std::min(0.0, v + dv);
    cmd_w_ = w > 0 ? std::max(0.0, w - dw) : std::min(0.0, w + dw);
    world_->drive(cmd_v_, cmd_w_);
    world_->step(dt);
    sync_from_world();
    velocity_ = std::abs(world_->v_fwd());
    angular_velocity_ = world_->yaw_rate();
    acceleration_ = velocity_ > 1e-9 ? -max_deceleration : 0.0;
    return get_state();
}

RobotKinematicState TrapezoidalKinematics::settle_straight(double dt) {
    // 이상 궤적은 목표에 닿았다. 실제 로봇이 목표 근처에 서 있을 때까지 기다리거나, 모자라면 마저 들어간다.
    // 직선으로는 옆 방향 오차를 못 고치므로 진행 방향 남은 거리만 본다.
    double along = (settle_->tx - x) * std::cos(theta) + (settle_->ty - y) * std::sin(theta);
    bool rest = world_->at_rest();
    if ((std::abs(along) <= kStraightAlong && rest) || settle_elapsed_ >= kSettleTimeout) {
        if (settle_elapsed_ >= kSettleTimeout && std::abs(along) > kStraightAlong)
            log::warning("straight settle timeout: %.3f m short of target, accepting", std::abs(along));
        settle_.reset();
        cmd_v_ = cmd_w_ = 0.0;
        return get_state();
    }
    if (std::abs(along) > kStraightAlong) {
        straight_ = Straight{settle_->tx, settle_->ty, std::min(settle_->max_speed, kSettleSpeed)};
        straight_goal_ = *straight_;
        straight_reverse_ = along < 0;
        settle_.reset();
        return step_physical(dt);
    }
    settle_elapsed_ += dt;
    return hold_physical(dt);
}

RobotKinematicState TrapezoidalKinematics::step_physical(double dt) {
    sync_from_world();
    if (settle_) return settle_straight(dt);

    const double x0 = x, y0 = y, th0 = theta;
    const bool was_straight = straight_.has_value();
    const Straight goal = straight_goal_;
    double aim_w = 0.0;
    if (was_straight) {
        // 로봇은 앞뒤로만 가므로 목표를 지금 heading 의 진행선 위로 투영한다 (옆 방향 오차는 직선으로 못 줄인다).
        const double hx = std::cos(th0), hy = std::sin(th0);
        const double along = (goal.tx - x0) * hx + (goal.ty - y0) * hy;
        if (std::abs(along) < kStraightAlong / 2) {  // 사실상 도착 -> 정착 대기
            straight_.reset();
            settle_ = goal;
            settle_elapsed_ = 0.0;
            velocity_ = 0.0;
            return hold_physical(dt);
        }
        straight_->tx = x0 + hx * along;
        straight_->ty = y0 + hy * along;
        // 멀면 목표 방위로 조향한다 (후진이면 등이 목표를 향하게). 가까우면 방위가 흔들리니 마지막 값을 유지.
        const double gx = goal.tx - x0, gy = goal.ty - y0;
        if (std::hypot(gx, gy) > kStraightAimDist)
            straight_theta_ = normalize_angle(std::atan2(gy, gx) + (straight_reverse_ ? kPi : 0.0));
        aim_w = std::clamp(kStraightHeadingGain * normalize_angle(straight_theta_ - th0), -max_angular_velocity,
                           max_angular_velocity);
    }

    {   // 이상 궤적의 속도 상태를 실제에 붙들어 맨다
        double vm = world_->v_fwd();
        double toward = std::max(0.0, (was_straight && straight_reverse_) ? -vm : vm);
        velocity_ = std::clamp(velocity_, toward - kVelocityBand, toward + kVelocityBand);
        double wm = world_->yaw_rate();
        angular_velocity_ = std::clamp(angular_velocity_, wm - kAngularBand, wm + kAngularBand);
    }
    step_ideal(dt);  // 측정 자세에서 이상 궤적을 한 걸음 낸다

    // 이상 궤적의 변위 -> 몸체 목표 속도. 로봇은 앞뒤로만 가므로 heading 방향 성분만 쓴다.
    double v_cmd = ((x - x0) * std::cos(th0) + (y - y0) * std::sin(th0)) / dt;
    double w_cmd = normalize_angle(theta - th0) / dt;
    if (was_straight) {
        w_cmd = aim_w;  // 직선 이동은 이상 궤적이 회전하지 않는다
        if (!straight_) {  // 이상 궤적이 끝났다 -> 정착 대기
            settle_ = goal;
            settle_elapsed_ = 0.0;
        }
    }
    cmd_v_ = v_cmd;
    cmd_w_ = w_cmd;
    world_->drive(v_cmd, w_cmd);
    world_->step(dt);
    sync_from_world();

    // 경로 끝에서 이상 궤적은 도착했는데 실제로는 못 닿았다 -> 마지막 지점을 다시 건다
    if (!was_straight && remaining_.empty() && goal_ &&
        std::hypot(goal_->first - x, goal_->second - y) > kArriveTolerance)
        remaining_.push_back(*goal_);
    return get_state();
}

}  // namespace fms
