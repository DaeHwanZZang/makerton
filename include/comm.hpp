// 로봇 MQTT 통신 (robot_client/comm.py 포팅). libmosquitto 사용.
#pragma once

#include <mosquitto.h>

#include <atomic>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <variant>

#include "schemas.hpp"

namespace fms {

// 시뮬 전용 순간이동 (sim/v1/{id}/teleport). 프로토콜 밖: 실제 로봇은 엘리베이터가 옮겨 준다.
// 참 자세만 옮기고 로봇은 UNLOCALIZED 가 된다 (이동한 줄 모른다).
struct SimTeleport {
    double x = 0, y = 0, theta = 0;
    std::string map_id;  // 선택. 지금 맵 uid 와 다르면 무시
};

using Incoming = std::variant<Order, InstantAction, SimTeleport>;

class RobotComm {
public:
    RobotComm(std::string robot_id, std::string host = "localhost", int port = 1883, int keepalive = 15);
    ~RobotComm();

    bool connect();  // false 면 접속 실패 (OSError 대응)
    void disconnect();
    void rebind_id(const std::string& new_robot_id);
    bool wait_until_connected(double timeout_sec = 5.0);
    bool is_connected() const { return connected_; }

    void publish_state(const State& state);
    void publish_physics(const PhysicsDebug& debug);  // 디버그 토픽 (뷰어 전용, 프로토콜 밖)
    void publish_online();
    long long next_header_id() { return ++header_id_; }

    std::vector<Incoming> drain();

    std::string robot_id() const { return robot_id_; }

private:
    std::string robot_id_, host_;
    int port_, keepalive_;
    long long header_id_ = 0;
    std::atomic<bool> connected_{false};
    mosquitto* mosq_ = nullptr;

    std::mutex mutex_;
    std::deque<Incoming> inbox_;

    void build_client();
    std::string connection_payload(ConnectionState state);

    static void on_connect(mosquitto*, void* userdata, int rc);
    static void on_disconnect(mosquitto*, void* userdata, int rc);
    static void on_message(mosquitto*, void* userdata, const mosquitto_message* msg);
};

}  // namespace fms
