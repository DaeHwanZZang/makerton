// 가상 라이다 + 스캔 매칭 (초기 로컬라이징).
//
// 시뮬 로봇은 참 자세를 알지만 "로봇" 은 모른다. 로컬라이징은 이렇게 흉내 낸다:
//   1. simulate_scan: 참 자세에서 맵으로 레이캐스트한 거리 측정 (로봇 좌표계 끝점, 노이즈 포함)
//   2. match: 위치 힌트 주변 창(±window_xy, ±window_theta)에서 그 스캔이 맵과 가장 잘 맞는 자세를 찾는다
// 힌트가 창 밖으로 틀리면 못 찾고(점수 미달), 대칭인 곳에서는 엉뚱한 자세로 잡힐 수도 있다 (실제와 같다).
//
// 점수: likelihood field. 맵의 벽 경계(FREE 와 맞닿은 비-FREE 픽셀)까지 거리 d 로 exp(-d²/2σ²) 를 미리 깔아 두고
// 스캔 끝점을 후보 자세로 옮겨 평균을 낸다 (0~1). 벽 "경계" 기준이라 회색 미탐색 덩어리 한가운데는 점수가 낮다.
// 탐색: 거친 격자(0.1 m, 2°, 끝점 솎음) -> 서로 떨어진 상위 3개 주변을 고운 격자(0.01 m, 0.25°, 끝점 전부)로.
//
// 이 클래스는 물리 엔진/executor 를 모른다. 맵 하나에 하나 만들고 맵이 바뀌면 버린다 (필드 생성이 수십~수백 ms).
#pragma once

#include <random>
#include <utility>
#include <vector>

#include "localizer.hpp"
#include "map_model.hpp"

namespace fms {

struct LidarParams {
    int beams = 360;
    double max_range = 20.0;   // m. 이보다 멀면 측정 없음
    double min_range = 0.05;   // m
    double sigma = 0.02;       // 거리 노이즈 표준편차 (m)
};

struct MatchParams {
    double window_xy = 1.5;       // 힌트에서 찾는 반경 (m, 정사각 창의 반폭)
    double window_theta = 0.5236; // 힌트에서 찾는 방위 범위 (rad, ±30°)
    double sigma_hit = 0.1;       // likelihood field 폭 (m)
    double min_score = 0.8;       // 이 점수 미만이면 실패 (맞는 자세는 보통 0.93 이상, 틀린 자세는 대개 0.7 미만)
    int min_points = 20;          // 측정 끝점이 이보다 적으면 실패 (주변에 벽이 안 보임)
    double prior_weight = 0.05;   // 힌트에서 멀수록 깎는 양 (점수가 비슷하면 힌트 쪽을 고른다)
};

struct MatchResult {
    bool ok = false;
    Pose2 pose;          // 찾은 자세 (ok 가 아니어도 최고점 자세)
    double score = 0.0;  // 0~1
    int points = 0;      // 쓴 스캔 끝점 수
};

class ScanMatcher {
public:
    explicit ScanMatcher(const MapModel& map, double field_res = 0.05, double sigma_hit = 0.1);

    // 참 자세에서 측정한 스캔. 로봇 좌표계(전방 +x, 왼쪽은 world y 가 아래로 증가하므로 +y 가 시계 방향 쪽) 끝점
    std::vector<std::pair<double, double>> simulate_scan(const Pose2& truth, const LidarParams& p,
                                                         std::mt19937& rng) const;
    MatchResult match(const std::vector<std::pair<double, double>>& scan, const Pose2& hint,
                      const MatchParams& p) const;
    // 한 자세의 점수 (0~1)
    double score(const std::vector<std::pair<double, double>>& scan, const Pose2& pose) const;

private:
    const MapModel& map_;
    int mw_ = 0, mh_ = 0;            // 맵 픽셀 크기
    double res_ = 0, ox_ = 0, oy_ = 0;
    double fres_ = 0.05;             // 필드 해상도 (m)
    int fw_ = 0, fh_ = 0;
    std::vector<float> field_;       // exp(-d²/2σ²), 경계 = 1

    double field_at(double x, double y) const;
};

}  // namespace fms
