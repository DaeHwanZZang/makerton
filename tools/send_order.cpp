// tools/send_order.py 포팅. 임시 FMS 대역 -- 오더/즉시명령 발행.
#include <iostream>
#include <sstream>

#include "mqtt_once.hpp"
#include "schemas.hpp"

using namespace fms;
using namespace fms::tools;

namespace {

std::vector<Pose> parse_path(const std::string& text) {
    // "x1,y1[,theta1] x2,y2[,theta2] ..." 형식. 콤마=좌표 구분, 공백=노드 구분. theta 는 라디안 (기본 0)
    std::vector<Pose> pts;
    std::stringstream ss(text);
    std::string tok;
    while (ss >> tok) {
        std::vector<double> v;
        std::stringstream ts(tok);
        std::string part;
        while (std::getline(ts, part, ',')) v.push_back(std::stod(part));
        if (v.size() != 2 && v.size() != 3) throw std::runtime_error("--path format is X,Y[,THETA]");
        pts.push_back(Pose{v[0], v[1], v.size() == 3 ? v[2] : 0.0});
    }
    return pts;
}

// --param KEY=VALUE. 숫자로 읽히면 number, true/false 면 bool, 나머지는 문자열
void add_param(Params& params, const std::string& kv) {
    auto eq = kv.find('=');
    if (eq == std::string::npos || eq == 0) throw std::runtime_error("--param format is KEY=VALUE");
    std::string k = kv.substr(0, eq), v = kv.substr(eq + 1);
    if (v == "true" || v == "false") { params[k] = (v == "true"); return; }
    try {
        size_t n;
        double d = std::stod(v, &n);
        if (n == v.size()) { params[k] = d; return; }
    } catch (...) {}
    params[k] = v;
}

void usage() {
    std::cout <<
        "Usage: send_order ROBOT_ID --path X,Y[,THETA] [X2,Y2[,THETA2] ...] --order-id ID [--update N] "
        "[--host H] [--port P]\n"
        "          [--action PICK|DROP|CHARGE|WAIT|STRAIGHT|DOCK|PARK [--duration S] [--param KEY=VALUE ...]]\n"
        "        (--action 은 마지막 노드에 붙는다. 예: --action DOCK --param entry=BACKWARD\n"
        "         --param undock_distance=0.6 --param min_charge=80 / --action STRAIGHT --param distance=-1.0)\n"
        "        send_order ROBOT_ID --instant CANCEL_ORDER|PAUSE|RESUME|RESET_ERROR|INJECT_FAULT [--fault NAME]\n"
        "        send_order ROBOT_ID --instant INIT_POSITION --param x=X --param y=Y [--param theta=RAD]  (위치 힌트)\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { usage(); return 1; }
    std::string robot_id = argv[1];
    std::string host = "localhost";
    int port = 1883;
    std::string path_text, order_id = "O1", instant_text, fault = "UNKNOWN_FAULT";
    int update_id = 0;
    std::string action_text;
    double duration = 0.0;
    Params params;

    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        auto next = [&]() { return std::string(argv[++i]); };
        if (k == "--host") host = next();
        else if (k == "--port") port = std::stoi(next());
        else if (k == "--path") {
            path_text.clear();
            while (i + 1 < argc && argv[i + 1][0] != '-') path_text += std::string(" ") + next();
        } else if (k == "--order-id") order_id = next();
        else if (k == "--update") update_id = std::stoi(next());
        else if (k == "--instant") instant_text = next();
        else if (k == "--fault") fault = next();
        else if (k == "--action") action_text = next();
        else if (k == "--duration") duration = std::stod(next());
        else if (k == "--param") add_param(params, next());
        else { usage(); return 1; }
    }

    OneShotPublisher pub(host, port);
    if (!pub.ok()) { std::cerr << "broker connection failed (" << host << ":" << port << ")\n"; return 1; }

    if (!instant_text.empty()) {
        InstantAction a;
        a.header_id = 1;
        a.timestamp = now_epoch();
        a.robot_id = robot_id;
        a.action_id = "IA-" + std::to_string((long long)(now_epoch() * 1000));
        a.action_type = instant_action_type_from(instant_text);
        if (a.action_type == InstantActionType::INJECT_FAULT) a.params["fault"] = fault;
        for (auto& [k, v] : params) a.params[k] = v;  // --param KEY=VALUE (예: INIT_POSITION 의 x, y, theta)
        pub.publish(topic_instant(robot_id), a.to_json_string());
        std::cout << "published -> " << topic_instant(robot_id) << "  " << instant_text << "\n";
        return 0;
    }

    if (path_text.empty()) { usage(); return 1; }
    auto pts = parse_path(path_text);
    Order order;
    order.header_id = 1;
    order.timestamp = now_epoch();
    order.robot_id = robot_id;
    order.order_id = order_id;
    order.order_update_id = update_id;
    for (size_t i = 0; i < pts.size(); ++i) {
        OrderNode n;
        n.node_id = "N" + std::to_string(i);
        n.sequence_id = (int)i;
        n.released = true;
        n.position = pts[i];
        order.nodes.push_back(n);
    }
    if (!action_text.empty()) {
        Action a;
        a.action_id = order_id + "-A" + std::to_string(pts.size() - 1);
        a.action_type = action_type_from(action_text);
        a.duration = duration;
        a.params = params;
        // 로봇과 같은 검증을 미리 돌린다 (잘못된 params 면 로봇이 오더를 통째로 버리므로)
        cJSON* aj = a.to_json();
        try { Action::from_json(aj); } catch (const ValidationError& e) {
            cJSON_Delete(aj);
            std::cerr << "invalid action: " << e.what() << "\n";
            return 1;
        }
        cJSON_Delete(aj);
        order.nodes.back().action = a;
    }

    // Order 에는 to_json_string 이 없다 (로봇은 만들 일이 없어서) -- 여기서만 직접 만든다
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "version", PROTOCOL_VERSION);
    cJSON_AddNumberToObject(root, "header_id", (double)order.header_id);
    cJSON_AddNumberToObject(root, "timestamp", order.timestamp);
    cJSON_AddStringToObject(root, "robot_id", order.robot_id.c_str());
    cJSON_AddStringToObject(root, "order_id", order.order_id.c_str());
    cJSON_AddNumberToObject(root, "order_update_id", order.order_update_id);
    cJSON_AddNullToObject(root, "task_id");
    cJSON* nodes = cJSON_CreateArray();
    for (auto& n : order.nodes) {
        cJSON* nd = cJSON_CreateObject();
        cJSON_AddStringToObject(nd, "node_id", n.node_id.c_str());
        cJSON_AddNumberToObject(nd, "sequence_id", n.sequence_id);
        cJSON_AddBoolToObject(nd, "released", n.released);
        cJSON_AddItemToObject(nd, "position", n.position.to_json());
        if (n.action) cJSON_AddItemToObject(nd, "action", n.action->to_json());
        else cJSON_AddNullToObject(nd, "action");
        cJSON_AddNullToObject(nd, "reserved_window");
        cJSON_AddItemToArray(nodes, nd);
    }
    cJSON_AddItemToObject(root, "nodes", nodes);
    char* txt = cJSON_PrintUnformatted(root);
    std::string payload(txt);
    free(txt);
    cJSON_Delete(root);

    pub.publish(topic_order(robot_id), payload);
    std::cout << "published -> " << topic_order(robot_id) << "\n  order_id=" << order_id
              << " update_id=" << update_id << "\n  " << pts.size() << " node(s)"
              << (action_text.empty() ? "" : ", last node action " + action_text) << "\n";
    return 0;
}
