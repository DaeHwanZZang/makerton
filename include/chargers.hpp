// 충전 스테이션 (맵에 딸린 물리 설비).
//
// 맵 폴더의 chargers.json 에 정의한다 (fms_map.json 옆. SET_MAP 번들이면 번들 안):
//   {"chargers": [{"id": "CH-1", "x": 30.0, "y": 6.0, "theta": 0.0, "entry": "BACKWARD"}]}
//   x, y, theta : 도킹 완료 자세 (= DOCK 노드 position 으로 줄 값)
//   entry       : 선택. 충전 접점이 있는 쪽으로 들어와야 한다 (FORWARD | BACKWARD). 다르면 접점이 안 닿는다
//   tol_xy, tol_theta : 선택. 접점이 닿는 허용 오차 (기본 0.06 m, 0.1 rad)
//
// 로봇은 DOCK 액션을 이 충전기 앞에서만 하고, 진입이 끝난 순간의 참 자세가 허용 오차 안이어야 충전된다.
// FMS 는 이 파일을 모른다 — FMS 쪽 충전기는 location(type=charger) 이다. 같은 자리를 가리키게 맞춰 둔다.
#pragma once

#include <string>
#include <vector>

namespace fms {

struct Charger {
    std::string id;
    double x = 0, y = 0, theta = 0;
    std::string entry;        // 비어 있으면 아무 쪽이나
    double tol_xy = 0.06;     // m
    double tol_theta = 0.1;   // rad
};

constexpr const char* CHARGERS_FILE = "chargers.json";

// map_dir/chargers.json 을 읽는다. 없으면 빈 목록, 형식이 틀리면 경고 로그 후 빈 목록
std::vector<Charger> load_chargers(const std::string& map_dir);

// (x, y, theta) 와 가장 가까운 충전기. 거리 radius, 방위차 max_dtheta 를 넘으면 nullptr
const Charger* find_charger(const std::vector<Charger>& chargers, double x, double y, double theta,
                            double radius = 0.3, double max_dtheta = 0.35);

}  // namespace fms
