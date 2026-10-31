// MuJoCo 물리 월드. 로봇 1대 (차동구동 + 캐스터 2개) 와 그리드 맵에서 만든 벽으로 이루어진다.
// MuJoCo 헤더는 physics_world.cpp 안에만 있다 (pimpl).
//
// 좌표계: 프로젝트 world 좌표를 그대로 쓴다 (x, y 미터, y 는 아래로 증가, theta = atan2(dy, dx)).
// 거울 대칭이라 물리 결과는 그대로 유효하다.
//
// 맵: FREE 가 아닌 칸(OCCUPIED/UNKNOWN, 맵 밖 포함)을 직사각형으로 병합해 벽 상자로 만든다.
// 맵 전체를 한 번에 올리지 않고 로봇 주변 창(window) 안의 상자만 모델에 넣는다. 로봇이 창 가장자리로
// 가면 모델을 다시 컴파일한다 (상태는 이어받는다). 그래서 맵이 커도 tick 비용이 일정하다.
#pragma once

#include <memory>

#include "localizer.hpp"
#include "map_model.hpp"

namespace fms {

struct RobotSpec {
    double width = 0.5, length = 0.7;  // 충돌 상자 크기 (m). 벽에 닿는 건 이 상자뿐이다
    double mass = 60.0;                // kg
    double wheel_radius = 0.075;       // m
    double friction = 0.8;             // 바퀴-바닥 마찰계수 μ
    double max_wheel_torque = 12.0;    // N·m, 바퀴 하나. 이걸 넘는 가속 요구는 바퀴가 못 따라간다
};

// 한 시점의 관측값. 학습 데이터 기록용 (recorder.cpp).
struct WorldTelemetry {
    double x = 0, y = 0, theta = 0;                // 실제(참) 자세 (물리 엔진 값)
    double odom_x = 0, odom_y = 0, odom_theta = 0;  // 바퀴 엔코더만 적분한 자세 (슬립이 쌓인다, 보정 전)
    double est_x = 0, est_y = 0, est_theta = 0;     // 로봇이 아는 자세 = 보정된 추정 (State.pose, 제어 기준)
    long loc_corrections = 0, loc_dropouts = 0;     // 위치추정 보정 횟수 / 빠진 측정 횟수
    double loc_step_xy = 0, loc_step_theta = 0;     // 마지막 보정이 추정을 옮긴 양
    double v_fwd = 0, v_lat = 0, yaw_rate = 0;     // 몸체 좌표계 속도 (전진 +, 왼쪽 +)
    double wheel_w[2] = {0, 0};                    // 바퀴 각속도 rad/s (왼쪽, 오른쪽)
    double wheel_cmd[2] = {0, 0};                  // 바퀴 목표 각속도
    double slip[2] = {0, 0};                       // 바퀴별 종방향 슬립률 (-1~1, 0 = 구름)
    double cmd_v = 0, cmd_w = 0;                   // 마지막으로 준 몸체 목표 속도
    double wall_force = 0;                         // 직전 step 에서 몸체-벽 접촉 최대 수직력 (N)
    bool ghost = false;                            // 시작부터 벽 안: 벽 충돌을 끈 상태
};

class PhysicsWorld {
public:
    PhysicsWorld(const RobotSpec& spec, double x, double y, double theta);
    ~PhysicsWorld();
    PhysicsWorld(const PhysicsWorld&) = delete;
    PhysicsWorld& operator=(const PhysicsWorld&) = delete;

    // 맵 교체. nullptr 이면 벽 없는 빈 바닥. 맵 객체는 소유하지 않고 다음 set_map 전까지 살아 있어야 한다.
    void set_map(const MapModel* map);
    // 로봇 크기/질량/마찰 변경 (모델을 다시 만든다)
    void set_spec(const RobotSpec& spec);
    void set_friction(double mu);  // 바닥 마찰만 바꾼다 (재컴파일 없음). 마찰 구역 용

    void teleport(double x, double y, double theta);  // 속도 0 으로 옮긴다
    void stop();                                      // 속도를 즉시 0 으로 (E-STOP, 취소)
    void drive(double v, double w);                   // 몸체 목표 속도 (전진 +, 반시계 +)
    void step(double dt);

    double x() const;
    double y() const;
    double theta() const;
    // 바퀴 엔코더만 적분한 자세 (슬립이 쌓여 실제와 벌어진다, 보정 전). teleport 하면 그 자세로 맞춰진다
    double odom_x() const;
    double odom_y() const;
    double odom_theta() const;
    // 로봇이 아는 자세 = 위치추정 보정(라이다 시뮬레이션)을 얹은 오도메트리. 보정이 꺼져 있으면 오도메트리와 같다.
    // 제어/경로추종/State.pose 는 이 값을 쓴다.
    double est_x() const;
    double est_y() const;
    double est_theta() const;
    // 위치추정 보정 설정 (rate_hz = 0 이면 끔). 보정 오프셋과 추적 여부는 유지한다
    void set_localization(const LocalizerParams& params);
    // 로봇이 아는 자세를 직접 정한다 (참 자세는 그대로). 위치 힌트 / 스캔 매칭 결과를 넣을 때
    void set_estimate(double x, double y, double theta);
    // 추적 보정 켬/끔. 로컬라이징 전에는 끈다 (localizer.hpp)
    void set_tracking(bool on);
    bool tracking() const;
    const LocalizerParams& localization() const;
    const LocalizerStats& loc_stats() const;
    double v_fwd() const;
    double yaw_rate() const;
    bool at_rest() const;      // 정지 판정 (속도 거의 0)
    bool collided() const;     // 벽에 눌린 채 오래 있었다 -> 충돌
    bool ghost() const;
    double wall_force() const;
    WorldTelemetry telemetry() const;
    const RobotSpec& spec() const;
    double track() const;      // 바퀴 간격 (m)

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace fms
