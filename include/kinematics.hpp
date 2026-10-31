// 로봇 운동 (robot_client/sim/kinematics.py 포팅)
// 등가속/등감속 사다리꼴 속도 프로파일로 "이상적인 궤적"을 만든다.
//
// PhysicsWorld 가 붙어 있으면(기본, OrderExecutor 가 붙인다) 이 궤적은 명령 생성기로만 쓴다:
// 매 tick 실제(물리) 자세에서 출발해 이상 궤적을 한 걸음 내고, 그 변위를 (v, w) 로 바꿔 바퀴에 준다.
// 위치는 물리 엔진이 정하므로 슬립/관성/벽 접촉이 그대로 나온다. 붙어 있지 않으면 순수 운동학이다.
#pragma once

#include <optional>
#include <utility>
#include <vector>

namespace fms {

class PhysicsWorld;

struct RobotKinematicState {
    double x, y, theta;
    double velocity, acceleration;
    double angular_velocity;
    std::vector<std::pair<double, double>> local_path;
    bool path_complete;
};

class TrapezoidalKinematics {
public:
    double x, y, theta;
    double max_velocity, max_acceleration, max_deceleration;
    double max_angular_velocity, max_angular_acceleration;

    TrapezoidalKinematics(double x_, double y_, double theta_ = 0.0, double max_v = 1.0,
                           double max_a = 0.5, double max_d = 0.5, double max_angular_v = 1.57,
                           double max_angular_a = 3.14)
        : x(x_), y(y_), theta(theta_), max_velocity(max_v), max_acceleration(max_a),
          max_deceleration(max_d), max_angular_velocity(max_angular_v), max_angular_acceleration(max_angular_a) {}

    // 경로가 바뀌면 최종 정렬 목표(final_theta_)는 해제한다. 필요하면 set_final_theta 로 다시 지정.
    void set_path(std::vector<std::pair<double, double>> waypoints) {
        goal_ = waypoints.empty() ? std::nullopt : std::optional(waypoints.back());
        remaining_ = std::move(waypoints);
        final_theta_.reset();
        straight_.reset();
        settle_.reset();
    }
    // 경로계획 없이 (tx, ty) 까지 직선 이동 (전/후진, DOCK/PARK 진입·탈출, STRAIGHT 액션).
    // 회전하지 않고 지금 heading 을 그대로 유지한다. 목표가 등 뒤면 후진이 된다.
    // 최고 속도는 min(max_speed, max_velocity). set_path 와 배타적이다 (서로를 지운다).
    void set_straight(double tx, double ty, double max_speed);
    // 직선 이동 중인가. 물리 모드에서는 이상 궤적이 끝난 뒤 실제 로봇이 목표에 서기까지(정착)도 true.
    bool straight_active() const { return straight_.has_value() || settle_.has_value(); }
    // 경로 마지막 지점 도착 후 이 각도로 정렬해야 is_path_complete() 가 true 가 된다.
    void set_final_theta(double target_theta) { final_theta_ = target_theta; }
    // 위치 강제 지정. 물리 모드에서는 로봇을 그 자리로 옮기고 속도를 0 으로 만든다.
    void set_pose(double nx, double ny, const double* ntheta = nullptr);
    // 물리 월드의 추정 자세를 다시 읽는다 (추정을 바깥에서 바꿨을 때: 위치 힌트/스캔 매칭)
    void resync() { if (world_) sync_from_world(); }
    // 즉시 정지. 목표를 모두 지운다 (물리 모드에서는 속도도 0).
    void emergency_stop();
    bool is_path_complete() const;

    // 물리 월드를 붙인다 (소유하지 않는다). nullptr 이면 순수 운동학.
    void attach_physics(PhysicsWorld* world);
    bool physical() const { return world_ != nullptr; }
    // 움직이라는 지령이 없을 때 부른다. 아직 굴러가고 있으면 감속시킨다 (물리 모드 전용, 아니면 아무 일 없음)
    void brake(double dt);
    // 몸체가 벽에 눌려 충돌로 판정됐는가 (물리 모드 전용)
    bool collided() const;

    RobotKinematicState hold(double dt);
    RobotKinematicState step(double dt);
    RobotKinematicState get_state() const;

    // 마지막 step 의 몸체 목표 속도 (기록용). 순수 운동학이면 0.
    double last_cmd_v() const { return cmd_v_; }
    double last_cmd_w() const { return cmd_w_; }

private:
    double velocity_ = 0.0;
    double acceleration_ = 0.0;
    double angular_velocity_ = 0.0;
    std::vector<std::pair<double, double>> remaining_;
    std::optional<double> final_theta_;
    struct Straight { double tx, ty, max_speed; };
    std::optional<Straight> straight_;

    // -- 물리 모드 --
    PhysicsWorld* world_ = nullptr;
    std::optional<std::pair<double, double>> goal_;  // 경로 마지막 지점. 이상 궤적이 끝났는데 실제로는 못 닿았으면 다시 건다
    double straight_theta_ = 0.0;                     // 직선 이동 중 유지할 heading
    Straight straight_goal_{0, 0, 0};                 // 직선 이동의 원래 목표. straight_ 목표는 매 tick 진행선 위로 투영한다
    bool straight_reverse_ = false;                   // 목표가 등 뒤 (후진)
    std::optional<Straight> settle_;                  // 직선 이동 정착 대기 (이상 궤적은 끝남)
    double settle_elapsed_ = 0.0;
    double cmd_v_ = 0.0, cmd_w_ = 0.0;

    void sync_from_world();
    RobotKinematicState step_ideal(double dt);
    RobotKinematicState hold_ideal(double dt);
    RobotKinematicState step_physical(double dt);
    RobotKinematicState settle_straight(double dt);
    RobotKinematicState hold_physical(double dt);

    double remaining_length() const;
    double stop_distance_ahead() const;
    void advance(double dist);
    // target_theta 로 사다리꼴 각가속도 프로파일을 따라 회전. 회전 후 남은 각도 오차 반환.
    double rotate_step(double target_theta, double dt);
    RobotKinematicState step_straight(double dt);
};

}  // namespace fms
