// 오더 실행기 (robot_client/executor.py 포팅). 상세 로직/주석은 원본 참고 —
// 여기서는 구조만 요약한다. 상태머신 규칙은 CLAUDE.md/robot_client.md 문서 그대로다.
#pragma once

#include <memory>
#include <optional>
#include <random>
#include <set>
#include <string>
#include <vector>

#include "battery.hpp"
#include "chargers.hpp"
#include "dynamic_obstacles.hpp"
#include "kinematics.hpp"
#include "map_model.hpp"
#include "path_refine.hpp"
#include "planner.hpp"
#include "physics_world.hpp"
#include "recorder.hpp"
#include "scan_matcher.hpp"
#include "schemas.hpp"

namespace fms {

class MapSync;  // map_sync.hpp -- SET_MAP 수신 시 다운로드를 맡긴다

class OrderExecutor {
public:
    std::string robot_id;
    double robot_width, robot_length;
    DynamicObstacleField obstacle_field;
    double sensor_range;
    std::unique_ptr<PhysicsWorld> physics;  // 몸체/벽 물리 (kinematics 가 가리킨다). kinematics 보다 먼저 선언해야 한다
    TrapezoidalKinematics kinematics;
    BatterySim battery;
    const MapModel* map = nullptr;  // 없으면 경로계획/충돌감지 꺼짐 (소유하지 않음)
    MapSync* map_sync = nullptr;    // 없으면 SET_MAP 은 로깅만 (소유하지 않음)
    PathRefineParams path_refine;   // enabled=false 가 기본 (A* 결과 그대로 사용)
    CenterlineParams centerline;    // 복도 중앙 주행. enabled=true 가 기본 (--no-center-path 로 끔)
    std::unique_ptr<Recorder> recorder;  // 있으면 tick 마다 한 줄 기록 (--record)
    // 로컬라이징 (가상 라이다 + 스캔 매칭). 힌트를 받고 localize_time 초 뒤에 매칭 결과로 끝난다
    LidarParams lidar;
    MatchParams match_params;
    double localize_time = 1.0;
    // 배터리 임계값 (%). low 아래면 WARNING LOW_BATTERY, critical 아래로 내려가는 순간 FATAL BATTERY_CRITICAL
    // (오더를 버린다), 0 이면 FATAL BATTERY_EMPTY + 구동 불가. critical 아래에서는 DOCK 오더만 받는다
    double battery_low = 20.0;
    double battery_critical = 5.0;
    std::mt19937 lidar_rng{1};  // main 이 --seed/로봇 id 로 다시 시드한다

    OrderExecutor(std::string robot_id_, double start_x, double start_y, double start_theta,
                  double max_velocity, double max_acceleration, BatterySim battery_,
                  const MapModel* map_model, double robot_width_, double robot_length_,
                  DynamicObstacleField obstacle_field_, double sensor_range_);

    // -- 상태 조회 --
    RobotState state() const { return estop_latched_ ? RobotState::EMERGENCY : base_state_; }
    bool motion_inhibited() const;
    bool can_accept_job() const;

    // -- 물리 패널 --
    std::string press_start();
    std::string press_stop();
    void set_estop(bool latched);
    bool toggle_estop();
    void set_mode(OperatingMode mode);
    bool pause(PauseSource source);
    bool resume(PauseSource by);
    void reset_error();

    // -- 오더/명령 --
    bool accept_order(const Order& order);
    void handle_instant(const InstantAction& action);
    void raise_fatal(ErrorInfo error);
    // 강제 로컬라이징: 로봇(참 자세)을 그 자세로 옮기고 바로 LOCALIZED (디버그/시뮬 전용, 오더를 버린다)
    bool force_relocalize(double x, double y, double theta = 0.0);
    // 시뮬 전용 순간이동 (엘리베이터가 옮겨 준 것): 참 자세만 옮기고 UNLOCALIZED 로 만든다. 힌트가 있어야 다시 주행한다
    bool sim_teleport(double x, double y, double theta, const std::string& map_uid = "");
    // 위치 힌트: 추정을 힌트로 두고 LOCALIZING 을 시작한다 (참 자세는 그대로). 맵이 없거나 맵 밖이면 거절
    bool localize_hint(double x, double y, double theta);
    LocalizationState localization() const { return localization_; }
    double localization_score() const { return loc_score_; }

    // 맵 교체 (map_sync 워커가 받아온 새 번들). 현재 위치 기준으로 경로 재계산.
    // uid/version 은 State 로 FMS 에 보고된다 (FMS 의 job 게이팅 기준).
    void set_map(const MapModel* new_map, std::string map_uid, std::string map_version);

    // GUI 에서 로봇 크기를 바꿨을 때 물리 상자도 맞춘다. friction/mass 가 0 보다 크면 그 값으로도 바꾼다
    void apply_robot_geometry(double friction = -1.0, double mass = -1.0);

    // -- 매 tick --
    void tick(double dt);

    State build_state(long long header_id, double timestamp) const;
    // 뷰어/GUI 디버그용 물리 관측값 (fms/v1/{id}/physics). 프로토콜이 아니다
    PhysicsDebug build_physics(long long header_id, double timestamp) const;

    // GUI/콘솔 설정 변경 (IDLE 일 때만 허용, main 이 호출)
    OperatingMode mode() const { return mode_; }
    bool paused() const { return paused_; }
    std::optional<std::string> order_id() const { return order_id_.empty() ? std::nullopt : std::optional(order_id_); }
    bool estop_latched() const { return estop_latched_; }
    std::optional<PauseSource> pause_source() const { return pause_source_; }

private:
    RobotState base_state_ = RobotState::IDLE;
    std::optional<BusySubState> sub_state_;
    bool estop_latched_ = false;
    bool paused_ = false;
    std::optional<PauseSource> pause_source_;
    OperatingMode mode_ = OperatingMode::AUTO;
    std::vector<ErrorInfo> errors_;

    // 복도 중앙 주행·경로 다듬기용 장애물 거리 장. 맵당 lazy 생성, set_map 에서 무효화.
    std::unique_ptr<ClearanceField> clearance_;

    // 로컬라이징 상태. UNLOCALIZED/LOCALIZING 이면 주행 억제 + 오더 거절 (motion_inhibited / can_accept_job)
    LocalizationState localization_ = LocalizationState::UNLOCALIZED;
    double loc_score_ = 0.0;
    std::optional<Pose2> loc_hint_;
    double loc_timer_ = 0.0;
    std::unique_ptr<ScanMatcher> matcher_;  // 맵당 lazy 생성 (likelihood field), set_map 에서 버린다
    void set_unlocalized(const char* why);
    void tick_localization(double dt);

    std::string current_map_uid_;      // 비어 있으면 State 에 map_id 안 실음
    std::string current_map_version_;

    std::string order_id_;
    int order_update_id_ = 0;
    std::vector<OrderNode> nodes_;
    int current_index_ = 0;
    std::string last_node_id_;
    int last_node_sequence_id_ = 0;
    std::optional<std::pair<std::string, int>> last_completed_;

    std::vector<int> segment_;
    // 이번 세그먼트 경로계획 결과. segment_wp_bounds_[k] = segment_[k] 노드에
    // 도달하는 시점의 누적 경유점 개수. 세그먼트 중간에서 멈췄다가(EMERGENCY/PAUSE/
    // MANUAL) 재개할 때 이미 지나온 노드로 되돌아가지 않도록 진행도를 추적한다.
    std::vector<size_t> segment_wp_bounds_;
    size_t segment_wp_total_ = 0;
    std::optional<Action> acting_;
    double acting_elapsed_ = 0.0;

    // 경로계획 없는 직선 이동 (STRAIGHT 액션, DOCK/PARK 진입, 언도킹). 있으면 tick 이 이것만 돌린다.
    // kinematics 쪽 직선 목표는 PAUSE/E-STOP 에서 지워지므로 여기 목표를 들고 있다가 재개 때 다시 건다.
    enum class StraightPurpose { ACTION, DOCK_IN, UNDOCK };
    struct StraightMotion { double tx, ty, speed; StraightPurpose purpose; };
    std::optional<StraightMotion> straight_;

    // 도킹/주차 상태. DOCK/PARK 진입을 시작할 때 생기고 언도킹이 끝나야 사라진다.
    // 진입이나 언도킹 도중에 오더가 취소돼도 남아 있어서, 다음 이동 전에 exit 지점까지 먼저 빠져나온다
    // (도크 안쪽은 planner 팽창 영역이라 A* 로는 못 나온다).
    struct Docked {
        bool charger;            // DOCK = true (충전), PARK = false
        std::optional<Charger> station;  // DOCK 대상 충전기 (chargers.json)
        std::string entry;       // 진입 방향 (FORWARD|BACKWARD) — 충전기 접점 방향과 맞아야 한다
        double exit_x, exit_y;   // 진입 직전 자세 = 언도킹 목표
        double min_charge;       // 이 충전량(%) 이상이어야 새 job 을 받는다 (DOCK 만)
        double speed;
        bool contact = false;    // 진입 완료 (충전 접점 연결)
    };
    std::optional<Docked> docked_;
    std::vector<std::pair<std::string, ActionStatus>> action_states_;  // 삽입 순서 유지

    std::vector<Charger> chargers_;   // 지금 맵의 충전기 (맵 폴더 chargers.json, set_map 때 다시 읽는다)
    double prev_charge_ = -1.0;       // 배터리 임계값 하향 통과 감지용 (음수 = 아직 모름)
    void check_battery();
    bool has_error(const char* type) const;
    void fail_action(const char* error_type, const std::string& desc);

    double sim_time_ = 0.0;  // 시작 후 누적 tick 시간 (기록용)
    double since_dynamic_replan_ = 0.0;
    std::set<std::string> detected_obstacle_ids_;
    std::vector<DynamicObstacle> last_detected_;

    void set_base(RobotState s) { base_state_ = s; sub_state_ = std::nullopt; }
    void set_busy(BusySubState sub) { base_state_ = RobotState::BUSY; sub_state_ = sub; }
    void resume_motion();
    bool apply_order_update(const Order& order);
    std::vector<DynamicObstacle> update_detections();
    bool path_blocked_by(const std::vector<DynamicObstacle>& detected) const;
    void try_dynamic_replan(const std::vector<DynamicObstacle>& detected);
    bool check_collision(double x, double y, double prev_x, double prev_y);
    void tick_logic(double dt);
    void record_sample(double dt);
    void tick_action(double dt);
    void tick_straight(double dt, const std::vector<DynamicObstacle>& detected);
    void finish_straight();
    void start_undock();
    bool docked_charging() const { return docked_ && docked_->charger && docked_->contact; }
    // 충전기에 붙어 있고 min_charge 미달 -> CHARGING 으로 보고하고 job 을 받지 않는다
    bool charge_hold() const { return docked_charging() && battery.charge < docked_->min_charge; }
    RobotState idle_state() const { return charge_hold() ? RobotState::CHARGING : RobotState::IDLE; }
    std::pair<double, double> approach_point(const OrderNode& node) const;
    void replan();
    std::vector<std::pair<double, double>> plan_segment_path(
        const std::vector<std::tuple<double, double, double, double>>* dynamic_obstacles = nullptr);
    void consume_reached_nodes();
    void on_segment_arrived();
    void clear_order();
    void set_action_status(const std::string& action_id, ActionStatus st);
    ActionStatus get_action_status(const std::string& action_id, ActionStatus def) const;
};

}  // namespace fms
