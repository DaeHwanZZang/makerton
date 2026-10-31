// tools/obstacle_zone.py 포팅. 실행 중인 로봇에 구역 통과형 장애물 시작/중지.
#include <iostream>
#include <sstream>

#include "mqtt_once.hpp"
#include "schemas.hpp"

using namespace fms;
using namespace fms::tools;

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cout << "Usage: obstacle_zone ROBOT_ID --zone XMIN,YMIN,XMAX,YMAX [--sides E-X] "
                     "[--spawn-interval S] [--max-count N] [--width W] [--height H] [--speed V] "
                     "| --stop\n";
        return 1;
    }
    std::string robot_id = argv[1];
    std::string host = "localhost";
    int port = 1883;
    std::string zone_text, sides_text;
    double spawn_interval = 5.0, width = 0.4, height = 0.4, speed = 0.5;
    int max_count = 3;
    bool do_stop = false;

    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        auto next = [&]() { return std::string(argv[++i]); };
        if (k == "--host") host = next();
        else if (k == "--port") port = std::stoi(next());
        else if (k == "--zone") zone_text = next();
        else if (k == "--sides") sides_text = next();
        else if (k == "--spawn-interval") spawn_interval = std::stod(next());
        else if (k == "--max-count") max_count = std::stoi(next());
        else if (k == "--width") width = std::stod(next());
        else if (k == "--height") height = std::stod(next());
        else if (k == "--speed") speed = std::stod(next());
        else if (k == "--stop") do_stop = true;
    }

    OneShotPublisher pub(host, port);
    if (!pub.ok()) { std::cerr << "broker connection failed\n"; return 1; }

    InstantAction a;
    a.header_id = 1;
    a.timestamp = now_epoch();
    a.robot_id = robot_id;
    a.action_id = "IA-" + std::to_string((long long)(now_epoch() * 1000));

    if (do_stop) {
        a.action_type = InstantActionType::STOP_OBSTACLE_ZONE;
        pub.publish(topic_instant(robot_id), a.to_json_string());
        std::cout << "published -> " << topic_instant(robot_id) << "  STOP_OBSTACLE_ZONE\n";
        return 0;
    }

    if (zone_text.empty()) { std::cerr << "--zone is required to start (use --stop to only stop)\n"; return 1; }
    std::vector<double> parts;
    std::stringstream ss(zone_text);
    std::string item;
    while (std::getline(ss, item, ',')) parts.push_back(std::stod(item));
    if (parts.size() != 4) { std::cerr << "--zone must be XMIN,YMIN,XMAX,YMAX\n"; return 1; }

    a.action_type = InstantActionType::START_OBSTACLE_ZONE;
    a.params["x_min"] = parts[0];
    a.params["y_min"] = parts[1];
    a.params["x_max"] = parts[2];
    a.params["y_max"] = parts[3];
    a.params["sides"] = sides_text;
    a.params["spawn_interval"] = spawn_interval;
    a.params["max_count"] = (double)max_count;
    a.params["width"] = width;
    a.params["height"] = height;
    a.params["speed"] = speed;
    pub.publish(topic_instant(robot_id), a.to_json_string());
    std::cout << "published -> " << topic_instant(robot_id) << "  START_OBSTACLE_ZONE  zone=" << zone_text
              << " sides=" << (sides_text.empty() ? "random" : sides_text) << "\n";
    return 0;
}
