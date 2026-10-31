// 학습/분석용 데이터 기록. tick 마다 한 줄씩 CSV 로 쓴다 (--record DIR).
// DIR/<robot_id>_<시작시각>.csv        시계열 (한 줄 = 한 tick, 50 ms)
// DIR/<robot_id>_<시작시각>.meta.json  실행 설정 (로봇 크기/질량/마찰, 맵, seed 등)
// 에피소드는 order_id 열로 나눈다 (order_id 가 비어 있으면 대기 중).
#pragma once

#include <cstdio>
#include <string>

#include "physics_world.hpp"

namespace fms {

struct RecordRow {
    double t = 0;                    // 로봇 시작 후 시뮬레이션 시간 (s)
    double wall_time = 0;            // epoch (s)
    std::string state, sub_state;    // RobotState / BusySubState
    std::string order_id;
    int node_index = 0;              // 오더에서 다음에 갈 노드 번호
    std::string action;              // 수행 중인 액션 종류 (없으면 빈 문자열)
    WorldTelemetry w;
    double battery = 0;
    bool charging = false;
    int detected_obstacles = 0;
    std::string error;               // 첫 오류 종류 (없으면 빈 문자열)
};

class Recorder {
public:
    // dir 를 만들고 파일을 연다. 실패하면 ok() 가 false.
    // meta_json: 파일에 그대로 쓸 JSON 객체 문자열 ({...})
    Recorder(const std::string& dir, const std::string& robot_id, const std::string& meta_json);
    ~Recorder();
    Recorder(const Recorder&) = delete;
    Recorder& operator=(const Recorder&) = delete;

    bool ok() const { return csv_ != nullptr; }
    const std::string& path() const { return csv_path_; }
    void write(const RecordRow& row);

private:
    std::FILE* csv_ = nullptr;
    std::string csv_path_;
    int since_flush_ = 0;
};

}  // namespace fms
