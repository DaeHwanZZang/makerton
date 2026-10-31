#include "map_model.hpp"

#include <cjson/cJSON.h>

#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#include "../third_party/stb_image.h"

namespace fms {

static std::string read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open file: " + path);
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

static std::string dirname_of(const std::string& path) {
    auto pos = path.find_last_of('/');
    if (pos == std::string::npos) return ".";
    return path.substr(0, pos);
}

MapMetadata MapMetadata::load(const std::string& json_path) {
    std::string text = read_file(json_path);
    cJSON* root = cJSON_Parse(text.c_str());
    if (!root) throw std::runtime_error("failed to parse map metadata JSON: " + json_path);

    MapMetadata m;
    auto get_str = [&](const char* k, const std::string& def) -> std::string {
        cJSON* f = cJSON_GetObjectItemCaseSensitive(root, k);
        return (f && cJSON_IsString(f)) ? f->valuestring : def;
    };
    auto get_num = [&](const char* k, double def) -> double {
        cJSON* f = cJSON_GetObjectItemCaseSensitive(root, k);
        return (f && cJSON_IsNumber(f)) ? f->valuedouble : def;
    };
    m.name = get_str("name", "map");
    cJSON* img = cJSON_GetObjectItemCaseSensitive(root, "image");
    if (!img || !cJSON_IsString(img)) { cJSON_Delete(root); throw std::runtime_error("image field is required"); }
    m.image = img->valuestring;
    cJSON* res = cJSON_GetObjectItemCaseSensitive(root, "resolution");
    if (!res || !cJSON_IsNumber(res) || res->valuedouble <= 0) {
        cJSON_Delete(root);
        throw std::runtime_error("resolution must be a positive number");
    }
    m.resolution = res->valuedouble;
    m.origin_x = get_num("origin_x", 0.0);
    m.origin_y = get_num("origin_y", 0.0);
    cJSON* wp = cJSON_GetObjectItemCaseSensitive(root, "width_px");
    cJSON* hp = cJSON_GetObjectItemCaseSensitive(root, "height_px");
    if (!wp || !hp) { cJSON_Delete(root); throw std::runtime_error("width_px/height_px required"); }
    m.width_px = (int)wp->valuedouble;
    m.height_px = (int)hp->valuedouble;
    m.occupied_thresh = (int)get_num("occupied_thresh", 64);
    m.free_thresh = (int)get_num("free_thresh", 192);
    cJSON_Delete(root);

    if (m.occupied_thresh >= m.free_thresh)
        throw std::runtime_error("occupied_thresh must be less than free_thresh");
    return m;
}

MapModel MapModel::load(const std::string& metadata_path) {
    MapModel model;
    model.metadata = MapMetadata::load(metadata_path);
    model.base_dir = dirname_of(metadata_path);
    return model;
}

std::pair<int, int> MapModel::world_to_pixel(double x, double y) const {
    const auto& m = metadata;
    int px = (int)std::floor((x - m.origin_x) / m.resolution);
    int py = (int)std::floor((y - m.origin_y) / m.resolution);
    return {px, py};
}

std::pair<double, double> MapModel::pixel_to_world(int px, int py) const {
    const auto& m = metadata;
    double x = m.origin_x + (px + 0.5) * m.resolution;
    double y = m.origin_y + (py + 0.5) * m.resolution;
    return {x, y};
}

bool MapModel::in_bounds(double x, double y) const {
    auto [px, py] = world_to_pixel(x, y);
    return px >= 0 && px < metadata.width_px && py >= 0 && py < metadata.height_px;
}

void MapModel::ensure_loaded() const {
    if (grid_loaded_) return;
    std::string path = base_dir + "/" + metadata.image;
    int w, h, channels;
    unsigned char* data = stbi_load(path.c_str(), &w, &h, &channels, 1);  // force grey
    if (!data) throw std::runtime_error("cannot read image: " + path);
    if (w != metadata.width_px || h != metadata.height_px) {
        stbi_image_free(data);
        throw std::runtime_error("image size does not match metadata");
    }
    grey_.assign(data, data + (size_t)w * h);
    stbi_image_free(data);

    classified_.resize(grey_.size());
    for (size_t i = 0; i < grey_.size(); ++i) {
        classified_[i] = (uint8_t)classify_grey(grey_[i]);
    }
    grid_loaded_ = true;
}

CellType MapModel::classify_grey(int grey) const {
    if (grey >= metadata.free_thresh) return CellType::FREE;
    if (grey <= metadata.occupied_thresh) return CellType::OCCUPIED;
    return CellType::UNKNOWN;
}

CellType MapModel::cell_at(double x, double y) const {
    if (!in_bounds(x, y)) return CellType::OCCUPIED;
    ensure_loaded();
    auto [px, py] = world_to_pixel(x, y);
    return static_cast<CellType>(classified_[(size_t)py * metadata.width_px + px]);
}

bool MapModel::is_free(double x, double y) const { return drivable(cell_at(x, y)); }

const std::vector<uint8_t>& MapModel::classify_grid() const {
    ensure_loaded();
    // classified_ 는 이미 0=OCCUPIED,1=UNKNOWN,2=FREE 코드값이다 (CellType enum 순서와 일치)
    return classified_;
}

}  // namespace fms
