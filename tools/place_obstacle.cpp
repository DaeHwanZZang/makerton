// tools/place_obstacle.py 포팅. 동적 장애물 수동 배치/제거 (INJECT_OBSTACLE/CLEAR_OBSTACLE).
#include <iostream>

#include "mqtt_once.hpp"
#include "schemas.hpp"

using namespace fms;
using namespace fms::tools;

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cout << "Usage: place_obstacle ROBOT_ID --x X --y Y [--width W] [--height H] "
                     "[--obstacle-id ID] | --clear [--obstacle-id ID]\n";
        return 1;
    }
    std::string robot_id = argv[1];
    std::string host = "localhost";
    int port = 1883;
    double x = 0, y = 0, width = 0.4, height = 0.4;
    std::string obstacle_id;
    bool do_clear = false;

    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        auto next = [&]() { return std::string(argv[++i]); };
        if (k == "--host") host = next();
        else if (k == "--port") port = std::stoi(next());
        else if (k == "--x") x = std::stod(next());
        else if (k == "--y") y = std::stod(next());
        else if (k == "--width") width = std::stod(next());
        else if (k == "--height") height = std::stod(next());
        else if (k == "--obstacle-id") obstacle_id = next();
        else if (k == "--clear") do_clear = true;
    }

    OneShotPublisher pub(host, port);
    if (!pub.ok()) { std::cerr << "broker connection failed\n"; return 1; }

    InstantAction a;
    a.header_id = 1;
    a.timestamp = now_epoch();
    a.robot_id = robot_id;
    a.action_id = "IA-" + std::to_string((long long)(now_epoch() * 1000));

    if (do_clear) {
        a.action_type = InstantActionType::CLEAR_OBSTACLE;
        if (!obstacle_id.empty()) a.params["obstacle_id"] = obstacle_id;
        pub.publish(topic_instant(robot_id), a.to_json_string());
        std::cout << "published -> " << topic_instant(robot_id) << "  CLEAR_OBSTACLE "
                  << (obstacle_id.empty() ? "(all)" : obstacle_id) << "\n";
        return 0;
    }

    a.action_type = InstantActionType::INJECT_OBSTACLE;
    std::string oid = obstacle_id.empty() ? ("MANUAL-" + std::to_string((long long)(now_epoch() * 1000))) : obstacle_id;
    a.params["obstacle_id"] = oid;
    a.params["x"] = x;
    a.params["y"] = y;
    a.params["width"] = width;
    a.params["height"] = height;
    pub.publish(topic_instant(robot_id), a.to_json_string());
    std::cout << "published -> " << topic_instant(robot_id) << "  INJECT_OBSTACLE (" << oid << ") ("
              << x << ", " << y << ") " << width << "x" << height << "\n";
    return 0;
}
