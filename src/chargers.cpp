#include "chargers.hpp"

#include <cjson/cJSON.h>

#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "log.hpp"

namespace fms {

std::vector<Charger> load_chargers(const std::string& map_dir) {
    std::vector<Charger> out;
    std::string path = (map_dir.empty() ? std::string(".") : map_dir) + "/" + CHARGERS_FILE;
    std::ifstream f(path);
    if (!f) return out;  // 충전기 없는 맵
    std::stringstream ss;
    ss << f.rdbuf();
    cJSON* root = cJSON_Parse(ss.str().c_str());
    cJSON* arr = root ? cJSON_GetObjectItemCaseSensitive(root, "chargers") : nullptr;
    if (!cJSON_IsArray(arr)) {
        log::warning("%s: \"chargers\" array missing -- no chargers loaded", path.c_str());
        cJSON_Delete(root);
        return out;
    }
    cJSON* item;
    cJSON_ArrayForEach(item, arr) {
        auto num = [&](const char* k, double def, bool required) {
            cJSON* v = cJSON_GetObjectItemCaseSensitive(item, k);
            if (cJSON_IsNumber(v)) return v->valuedouble;
            if (required) throw std::runtime_error(std::string("number ") + k + " required");
            return def;
        };
        try {
            Charger c;
            cJSON* id = cJSON_GetObjectItemCaseSensitive(item, "id");
            c.id = cJSON_IsString(id) ? id->valuestring : "CH-" + std::to_string(out.size() + 1);
            c.x = num("x", 0, true);
            c.y = num("y", 0, true);
            c.theta = std::atan2(std::sin(num("theta", 0, false)), std::cos(num("theta", 0, false)));
            cJSON* e = cJSON_GetObjectItemCaseSensitive(item, "entry");
            if (cJSON_IsString(e)) {
                c.entry = e->valuestring;
                if (c.entry != "FORWARD" && c.entry != "BACKWARD")
                    throw std::runtime_error("entry must be FORWARD|BACKWARD");
            }
            c.tol_xy = num("tol_xy", c.tol_xy, false);
            c.tol_theta = num("tol_theta", c.tol_theta, false);
            out.push_back(std::move(c));
        } catch (const std::exception& ex) {
            log::warning("%s: charger #%zu ignored: %s", path.c_str(), out.size() + 1, ex.what());
        }
    }
    cJSON_Delete(root);
    log::info("chargers loaded: %zu from %s", out.size(), path.c_str());
    return out;
}

const Charger* find_charger(const std::vector<Charger>& chargers, double x, double y, double theta, double radius,
                            double max_dtheta) {
    const Charger* best = nullptr;
    double best_d = radius;
    for (auto& c : chargers) {
        double d = std::hypot(c.x - x, c.y - y);
        double dth = std::abs(std::atan2(std::sin(c.theta - theta), std::cos(c.theta - theta)));
        if (d <= best_d && dth <= max_dtheta) {
            best = &c;
            best_d = d;
        }
    }
    return best;
}

}  // namespace fms
