#include "comm.hpp"

#include <chrono>
#include <cstring>
#include <thread>

#include "log.hpp"

namespace fms {

RobotComm::RobotComm(std::string robot_id, std::string host, int port, int keepalive)
    : robot_id_(std::move(robot_id)), host_(std::move(host)), port_(port), keepalive_(keepalive) {
    static bool lib_inited = false;
    if (!lib_inited) { mosquitto_lib_init(); lib_inited = true; }
    build_client();
}

RobotComm::~RobotComm() {
    if (mosq_) mosquitto_destroy(mosq_);
}

std::string RobotComm::connection_payload(ConnectionState state) {
    Connection c;
    c.header_id = next_header_id();
    c.timestamp = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    c.robot_id = robot_id_;
    c.connection_state = state;
    return c.to_json_string();
}

void RobotComm::build_client() {
    std::string client_id = "robot-" + robot_id_;
    mosq_ = mosquitto_new(client_id.c_str(), true, this);
    mosquitto_connect_callback_set(mosq_, &RobotComm::on_connect);
    mosquitto_disconnect_callback_set(mosq_, &RobotComm::on_disconnect);
    mosquitto_message_callback_set(mosq_, &RobotComm::on_message);

    std::string will = connection_payload(ConnectionState::CONNECTION_BROKEN);
    mosquitto_will_set(mosq_, topic_connection(robot_id_).c_str(), (int)will.size(), will.c_str(), 1, true);
}

bool RobotComm::connect() {
    int rc = mosquitto_connect(mosq_, host_.c_str(), port_, keepalive_);
    if (rc != MOSQ_ERR_SUCCESS) return false;
    mosquitto_loop_start(mosq_);
    return true;
}

bool RobotComm::wait_until_connected(double timeout_sec) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout_sec);
    while (std::chrono::steady_clock::now() < deadline) {
        if (connected_) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    return false;
}

void RobotComm::disconnect() {
    std::string payload = connection_payload(ConnectionState::OFFLINE);
    mosquitto_publish(mosq_, nullptr, topic_connection(robot_id_).c_str(), (int)payload.size(),
                       payload.c_str(), 1, true);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));  // wait_for_publish 근사
    mosquitto_loop_stop(mosq_, false);
    mosquitto_disconnect(mosq_);
}

void RobotComm::rebind_id(const std::string& new_robot_id) {
    if (new_robot_id == robot_id_) return;
    std::string old_id = robot_id_;
    std::string payload = connection_payload(ConnectionState::OFFLINE);
    mosquitto_publish(mosq_, nullptr, topic_connection(old_id).c_str(), (int)payload.size(), payload.c_str(),
                       1, true);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    mosquitto_loop_stop(mosq_, false);
    mosquitto_disconnect(mosq_);
    mosquitto_destroy(mosq_);

    robot_id_ = new_robot_id;
    build_client();
    connect();
    log::info("[%s] ID change complete (was: %s)", new_robot_id.c_str(), old_id.c_str());
}

void RobotComm::publish_state(const State& state) {
    std::string payload = state.to_json_string();
    mosquitto_publish(mosq_, nullptr, topic_state(robot_id_).c_str(), (int)payload.size(), payload.c_str(), 0,
                       false);
}

void RobotComm::publish_physics(const PhysicsDebug& debug) {
    std::string payload = debug.to_json_string();
    mosquitto_publish(mosq_, nullptr, topic_physics(robot_id_).c_str(), (int)payload.size(), payload.c_str(), 0,
                       false);
}

void RobotComm::publish_online() {
    std::string payload = connection_payload(ConnectionState::ONLINE);
    mosquitto_publish(mosq_, nullptr, topic_connection(robot_id_).c_str(), (int)payload.size(),
                       payload.c_str(), 1, true);
}

std::vector<Incoming> RobotComm::drain() {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<Incoming> items(inbox_.begin(), inbox_.end());
    inbox_.clear();
    return items;
}

void RobotComm::on_connect(mosquitto*, void* userdata, int rc) {
    auto* self = static_cast<RobotComm*>(userdata);
    if (rc != 0) {
        log::error("[%s] connection failed: %d", self->robot_id_.c_str(), rc);
        return;
    }
    self->connected_ = true;
    std::string ot = topic_order(self->robot_id_), it = topic_instant(self->robot_id_);
    mosquitto_subscribe(self->mosq_, nullptr, ot.c_str(), 1);
    mosquitto_subscribe(self->mosq_, nullptr, it.c_str(), 1);
    std::string tt = topic_sim_teleport(self->robot_id_);
    mosquitto_subscribe(self->mosq_, nullptr, tt.c_str(), 1);
    self->publish_online();
    log::info("[%s] connected to broker %s:%d", self->robot_id_.c_str(), self->host_.c_str(), self->port_);
}

void RobotComm::on_disconnect(mosquitto*, void* userdata, int rc) {
    auto* self = static_cast<RobotComm*>(userdata);
    self->connected_ = false;
    if (rc != 0) log::warning("[%s] disconnected: %d (retrying)", self->robot_id_.c_str(), rc);
}

void RobotComm::on_message(mosquitto*, void* userdata, const mosquitto_message* msg) {
    auto* self = static_cast<RobotComm*>(userdata);
    if (!msg->payload || msg->payloadlen <= 0) return;
    std::string payload((const char*)msg->payload, (size_t)msg->payloadlen);
    std::string topic = msg->topic;
    try {
        if (topic == topic_order(self->robot_id_)) {
            Order o = Order::from_json_string(payload);
            std::lock_guard<std::mutex> lock(self->mutex_);
            self->inbox_.push_back(std::move(o));
        } else if (topic == topic_instant(self->robot_id_)) {
            InstantAction a = InstantAction::from_json_string(payload);
            std::lock_guard<std::mutex> lock(self->mutex_);
            self->inbox_.push_back(std::move(a));
        } else if (topic == topic_sim_teleport(self->robot_id_)) {
            cJSON* j = cJSON_Parse(payload.c_str());
            if (!j) throw ValidationError("teleport: invalid JSON");
            cJSON* x = cJSON_GetObjectItemCaseSensitive(j, "x");
            cJSON* y = cJSON_GetObjectItemCaseSensitive(j, "y");
            cJSON* th = cJSON_GetObjectItemCaseSensitive(j, "theta");
            cJSON* mid = cJSON_GetObjectItemCaseSensitive(j, "map_id");
            if (!cJSON_IsNumber(x) || !cJSON_IsNumber(y)) {
                cJSON_Delete(j);
                throw ValidationError("teleport: x/y required");
            }
            SimTeleport t;
            t.x = x->valuedouble;
            t.y = y->valuedouble;
            t.theta = cJSON_IsNumber(th) ? th->valuedouble : 0.0;
            if (cJSON_IsString(mid)) t.map_id = mid->valuestring;
            cJSON_Delete(j);
            std::lock_guard<std::mutex> lock(self->mutex_);
            self->inbox_.push_back(std::move(t));
        } else {
            log::debug("[%s] unsubscribed topic: %s", self->robot_id_.c_str(), topic.c_str());
        }
    } catch (const ValidationError& e) {
        log::error("[%s] ignoring invalid message (%s): %s", self->robot_id_.c_str(), topic.c_str(), e.what());
    }
}

}  // namespace fms
