// A* 경로 후처리: PSO 로 중간 경유점을 미세 조정해 회전/장애물 근접을 줄인다.
// 경유점 개수와 마지막(목표) 점은 바꾸지 않는다 -- executor 의 segment_wp_bounds_
// (노드별 누적 경유점 개수) 진행도 추적이 깨지지 않게 하기 위함.
#pragma once

#include <utility>
#include <vector>

#include "map_model.hpp"

namespace fms {

struct PathRefineParams {
    bool enabled = false;
    double max_offset = 0.5;   // 경유점 하나가 A* 위치에서 움직일 수 있는 최대 거리 (x, y 각각, m)
    int particles = 20;
    int iterations = 40;
    double safe_margin = 0.15;  // 로봇 반경 밖으로 추가로 확보하고 싶은 여유 (m). 소프트 페널티
    unsigned seed = 0;
};

// 장애물(FREE 가 아닌 셀)까지의 유클리드 거리 장. 맵당 한 번 만든다.
class ClearanceField {
public:
    explicit ClearanceField(const MapModel& map);

    // (x, y) 가 속한 픽셀에서 가장 가까운 비-FREE 셀까지의 거리 (픽셀 단위). 범위 밖이면 0.
    double dist_px(double x, double y) const;
    double resolution() const { return resolution_; }

private:
    const MapModel* map_;
    int w_, h_;
    double resolution_;
    std::vector<float> dist_;  // 픽셀 단위 거리, index = py*w+px
};

// path: A* 결과 (start 제외, 목표 포함). 같은 길이의 경로를 돌려준다.
// 후보가 기준(A*) 경로보다 충돌 샘플이 많으면 원본을 그대로 돌려준다.
std::vector<std::pair<double, double>> refine_path(
    const MapModel& map, const ClearanceField& field, std::pair<double, double> start,
    const std::vector<std::pair<double, double>>& path, double robot_width,
    const PathRefineParams& params);

}  // namespace fms
