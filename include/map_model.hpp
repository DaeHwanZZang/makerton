// 맵 데이터 모델 (common/map_model.py 포팅)
// 좌표계: world 는 미터, origin 은 이미지 좌상단 픽셀의 world 좌표.
// y 축은 뒤집지 않는다 (world y 도 아래로 증가) — 벤더 맵 포맷에 맞춘 것.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace fms {

// 값 자체가 python classify_grid() 의 코드값과 같다: 0=OCCUPIED, 1=UNKNOWN, 2=FREE
enum class CellType { OCCUPIED = 0, UNKNOWN = 1, FREE = 2 };
inline bool drivable(CellType c) { return c == CellType::FREE; }

struct MapMetadata {
    std::string name = "map";
    std::string image;
    double resolution = 0.05;
    double origin_x = 0.0, origin_y = 0.0;
    int width_px = 0, height_px = 0;
    int occupied_thresh = 64;
    int free_thresh = 192;

    static MapMetadata load(const std::string& json_path);  // throws std::runtime_error
    double width_m() const { return width_px * resolution; }
    double height_m() const { return height_px * resolution; }
};

class MapModel {
public:
    MapMetadata metadata;
    std::string base_dir;

    static MapModel load(const std::string& metadata_path);

    std::pair<int, int> world_to_pixel(double x, double y) const;
    std::pair<double, double> pixel_to_world(int px, int py) const;
    bool in_bounds(double x, double y) const;

    CellType cell_at(double x, double y) const;
    bool is_free(double x, double y) const;

    // 0=OCCUPIED, 1=UNKNOWN, 2=FREE. shape: height_px*width_px, index = py*width_px+px
    const std::vector<uint8_t>& classify_grid() const;

private:
    mutable std::vector<uint8_t> grey_;       // lazy: 원본 그레이스케일
    mutable std::vector<uint8_t> classified_;  // lazy: 0/1/2 분류
    mutable bool grid_loaded_ = false;

    void ensure_loaded() const;
    CellType classify_grey(int grey) const;
};

}  // namespace fms
