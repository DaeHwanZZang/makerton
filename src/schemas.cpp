#include "schemas.hpp"

#include <cmath>
#include <set>
#include <unordered_set>

namespace fms {

// -- enum <-> string ----------------------------------------------------

std::string to_string(RobotState v) {
    switch (v) {
        case RobotState::IDLE: return "IDLE";
        case RobotState::BUSY: return "BUSY";
        case RobotState::CHARGING: return "CHARGING";
        case RobotState::ERROR: return "ERROR";
        case RobotState::EMERGENCY: return "EMERGENCY";
    }
    return "IDLE";
}
std::string to_string(BusySubState v) {
    switch (v) {
        case BusySubState::MOVING: return "MOVING";
        case BusySubState::WAITING: return "WAITING";
        case BusySubState::ACTING: return "ACTING";
    }
    return "MOVING";
}
std::string to_string(OperatingMode v) { return v == OperatingMode::AUTO ? "AUTO" : "MANUAL"; }
std::string to_string(PauseSource v) { return v == PauseSource::LOCAL ? "LOCAL" : "FMS"; }
std::string to_string(ActionType v) {
    switch (v) {
        case ActionType::PICK: return "PICK";
        case ActionType::DROP: return "DROP";
        case ActionType::CHARGE: return "CHARGE";
        case ActionType::WAIT: return "WAIT";
        case ActionType::STRAIGHT: return "STRAIGHT";
        case ActionType::DOCK: return "DOCK";
        case ActionType::PARK: return "PARK";
    }
    return "WAIT";
}
std::string to_string(ActionStatus v) {
    switch (v) {
        case ActionStatus::WAITING: return "WAITING";
        case ActionStatus::RUNNING: return "RUNNING";
        case ActionStatus::FINISHED: return "FINISHED";
        case ActionStatus::FAILED: return "FAILED";
    }
    return "WAITING";
}
std::string to_string(ErrorLevel v) { return v == ErrorLevel::WARNING ? "WARNING" : "FATAL"; }
std::string to_string(ConnectionState v) {
    switch (v) {
        case ConnectionState::ONLINE: return "ONLINE";
        case ConnectionState::OFFLINE: return "OFFLINE";
        case ConnectionState::CONNECTION_BROKEN: return "CONNECTION_BROKEN";
    }
    return "OFFLINE";
}
std::string to_string(LocalizationState v) {
    switch (v) {
        case LocalizationState::UNLOCALIZED: return "UNLOCALIZED";
        case LocalizationState::LOCALIZING: return "LOCALIZING";
        case LocalizationState::LOCALIZED: return "LOCALIZED";
    }
    return "UNLOCALIZED";
}
std::string to_string(InstantActionType v) {
    switch (v) {
        case InstantActionType::CANCEL_ORDER: return "CANCEL_ORDER";
        case InstantActionType::PAUSE: return "PAUSE";
        case InstantActionType::RESUME: return "RESUME";
        case InstantActionType::RESET_ERROR: return "RESET_ERROR";
        case InstantActionType::SET_MAP: return "SET_MAP";
        case InstantActionType::INJECT_FAULT: return "INJECT_FAULT";
        case InstantActionType::INJECT_OBSTACLE: return "INJECT_OBSTACLE";
        case InstantActionType::CLEAR_OBSTACLE: return "CLEAR_OBSTACLE";
        case InstantActionType::START_OBSTACLE_ZONE: return "START_OBSTACLE_ZONE";
        case InstantActionType::STOP_OBSTACLE_ZONE: return "STOP_OBSTACLE_ZONE";
        case InstantActionType::INIT_POSITION: return "INIT_POSITION";
    }
    return "PAUSE";
}

RobotState robot_state_from(const std::string& s) {
    if (s == "IDLE") return RobotState::IDLE;
    if (s == "BUSY") return RobotState::BUSY;
    if (s == "CHARGING") return RobotState::CHARGING;
    if (s == "ERROR") return RobotState::ERROR;
    if (s == "EMERGENCY") return RobotState::EMERGENCY;
    throw ValidationError("unknown RobotState: " + s);
}
BusySubState busy_sub_state_from(const std::string& s) {
    if (s == "MOVING") return BusySubState::MOVING;
    if (s == "WAITING") return BusySubState::WAITING;
    if (s == "ACTING") return BusySubState::ACTING;
    throw ValidationError("unknown BusySubState: " + s);
}
OperatingMode operating_mode_from(const std::string& s) {
    if (s == "AUTO") return OperatingMode::AUTO;
    if (s == "MANUAL") return OperatingMode::MANUAL;
    throw ValidationError("unknown OperatingMode: " + s);
}
PauseSource pause_source_from(const std::string& s) {
    if (s == "LOCAL") return PauseSource::LOCAL;
    if (s == "FMS") return PauseSource::FMS;
    throw ValidationError("unknown PauseSource: " + s);
}
ActionType action_type_from(const std::string& s) {
    if (s == "PICK") return ActionType::PICK;
    if (s == "DROP") return ActionType::DROP;
    if (s == "CHARGE") return ActionType::CHARGE;
    if (s == "WAIT") return ActionType::WAIT;
    if (s == "STRAIGHT") return ActionType::STRAIGHT;
    if (s == "DOCK") return ActionType::DOCK;
    if (s == "PARK") return ActionType::PARK;
    throw ValidationError("unknown ActionType: " + s);
}
ActionStatus action_status_from(const std::string& s) {
    if (s == "WAITING") return ActionStatus::WAITING;
    if (s == "RUNNING") return ActionStatus::RUNNING;
    if (s == "FINISHED") return ActionStatus::FINISHED;
    if (s == "FAILED") return ActionStatus::FAILED;
    throw ValidationError("unknown ActionStatus: " + s);
}
ErrorLevel error_level_from(const std::string& s) {
    if (s == "WARNING") return ErrorLevel::WARNING;
    if (s == "FATAL") return ErrorLevel::FATAL;
    throw ValidationError("unknown ErrorLevel: " + s);
}
ConnectionState connection_state_from(const std::string& s) {
    if (s == "ONLINE") return ConnectionState::ONLINE;
    if (s == "OFFLINE") return ConnectionState::OFFLINE;
    if (s == "CONNECTION_BROKEN") return ConnectionState::CONNECTION_BROKEN;
    throw ValidationError("unknown ConnectionState: " + s);
}
LocalizationState localization_state_from(const std::string& s) {
    if (s == "UNLOCALIZED") return LocalizationState::UNLOCALIZED;
    if (s == "LOCALIZING") return LocalizationState::LOCALIZING;
    if (s == "LOCALIZED") return LocalizationState::LOCALIZED;
    throw ValidationError("unknown LocalizationState: " + s);
}
InstantActionType instant_action_type_from(const std::string& s) {
    if (s == "CANCEL_ORDER") return InstantActionType::CANCEL_ORDER;
    if (s == "PAUSE") return InstantActionType::PAUSE;
    if (s == "RESUME") return InstantActionType::RESUME;
    if (s == "RESET_ERROR") return InstantActionType::RESET_ERROR;
    if (s == "SET_MAP") return InstantActionType::SET_MAP;
    if (s == "INJECT_FAULT") return InstantActionType::INJECT_FAULT;
    if (s == "INJECT_OBSTACLE") return InstantActionType::INJECT_OBSTACLE;
    if (s == "CLEAR_OBSTACLE") return InstantActionType::CLEAR_OBSTACLE;
    if (s == "START_OBSTACLE_ZONE") return InstantActionType::START_OBSTACLE_ZONE;
    if (s == "STOP_OBSTACLE_ZONE") return InstantActionType::STOP_OBSTACLE_ZONE;
    if (s == "INIT_POSITION") return InstantActionType::INIT_POSITION;
    throw ValidationError("unknown InstantActionType: " + s);
}

// -- json 파싱 헬퍼 (extra=forbid 재현용) --------------------------------

namespace {

void require_no_extra_fields(cJSON* obj, const std::unordered_set<std::string>& known,
                              const std::string& ctx) {
    cJSON* child = obj->child;
    while (child) {
        if (known.find(child->string ? child->string : "") == known.end()) {
            throw ValidationError(ctx + ": unknown field '" +
                                   (child->string ? child->string : "?") + "' (extra=forbid)");
        }
        child = child->next;
    }
}

cJSON* require_field(cJSON* obj, const char* name, const std::string& ctx) {
    cJSON* f = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!f) throw ValidationError(ctx + ": missing required field '" + name + "'");
    return f;
}

double get_number(cJSON* obj, const char* name, const std::string& ctx) {
    cJSON* f = require_field(obj, name, ctx);
    if (!cJSON_IsNumber(f)) throw ValidationError(ctx + ": '" + name + "' must be a number");
    return f->valuedouble;
}

double get_number_opt(cJSON* obj, const char* name, double def) {
    cJSON* f = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!f || cJSON_IsNull(f)) return def;
    return f->valuedouble;
}

std::string get_str(cJSON* obj, const char* name, const std::string& ctx) {
    cJSON* f = require_field(obj, name, ctx);
    if (!cJSON_IsString(f)) throw ValidationError(ctx + ": '" + name + "' must be a string");
    return f->valuestring;
}

std::string get_str_opt(cJSON* obj, const char* name, const std::string& def) {
    cJSON* f = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!f || cJSON_IsNull(f) || !cJSON_IsString(f)) return def;
    return f->valuestring;
}

bool get_bool(cJSON* obj, const char* name, const std::string& ctx) {
    cJSON* f = require_field(obj, name, ctx);
    if (!cJSON_IsBool(f)) throw ValidationError(ctx + ": '" + name + "' must be a bool");
    return cJSON_IsTrue(f);
}

long long get_int(cJSON* obj, const char* name, const std::string& ctx) {
    return static_cast<long long>(get_number(obj, name, ctx));
}

}  // namespace

// -- 값 객체 to_json ------------------------------------------------------

cJSON* Pose::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "x", x);
    cJSON_AddNumberToObject(o, "y", y);
    cJSON_AddNumberToObject(o, "theta", theta);
    return o;
}

static Pose pose_from_json(cJSON* obj) {
    static const std::unordered_set<std::string> known = {"x", "y", "theta"};
    require_no_extra_fields(obj, known, "Pose");
    Pose p;
    p.x = get_number(obj, "x", "Pose");
    p.y = get_number(obj, "y", "Pose");
    p.theta = get_number(obj, "theta", "Pose");
    if (p.theta < -3.15 || p.theta > 3.15) throw ValidationError("Pose.theta out of range (-3.15~3.15)");
    return p;
}

// -- params (Action/InstantAction 공용) ------------------------------------

namespace {

Params parse_params(cJSON* params, const char* ctx) {
    Params out;
    if (!params || cJSON_IsNull(params)) return out;
    if (!cJSON_IsObject(params)) throw ValidationError(std::string(ctx) + ".params must be an object");
    for (cJSON* child = params->child; child; child = child->next) {
        std::string key = child->string ? child->string : "";
        if (cJSON_IsBool(child)) out[key] = (bool)cJSON_IsTrue(child);
        else if (cJSON_IsNumber(child)) out[key] = child->valuedouble;
        else if (cJSON_IsString(child)) out[key] = std::string(child->valuestring);
        else throw ValidationError(std::string(ctx) + ".params values must be float|str|bool");
    }
    return out;
}

cJSON* params_to_json(const Params& params) {
    cJSON* p = cJSON_CreateObject();
    for (auto& [k, v] : params) {
        if (std::holds_alternative<double>(v)) cJSON_AddNumberToObject(p, k.c_str(), std::get<double>(v));
        else if (std::holds_alternative<bool>(v)) cJSON_AddBoolToObject(p, k.c_str(), std::get<bool>(v));
        else cJSON_AddStringToObject(p, k.c_str(), std::get<std::string>(v).c_str());
    }
    return p;
}

// 숫자로 읽을 수 있는 필수 param 인가 (문자열 숫자도 허용 — param_double 과 같은 관대함)
bool param_is_number(const Params& p, const std::string& key) {
    auto it = p.find(key);
    if (it == p.end()) return false;
    if (std::holds_alternative<double>(it->second)) return std::isfinite(std::get<double>(it->second));
    if (std::holds_alternative<std::string>(it->second)) {
        try { size_t n; return std::isfinite(std::stod(std::get<std::string>(it->second), &n)) && n > 0; } catch (...) { return false; }
    }
    return false;
}

// STRAIGHT/DOCK/PARK 는 params 없이는 실행할 수 없다. 잘못된 오더는 통째로 버린다
// (duration < 0 과 같은 취급 — 반쯤 실행하다 멈추는 것보다 받지 않는 게 안전하다).
void check_action_params(const Action& a) {
    auto need_number = [&](const char* key) {
        if (!param_is_number(a.params, key))
            throw ValidationError("Action " + to_string(a.action_type) + " requires numeric params." + key);
        return param_double(a.params, key, 0.0);
    };
    auto check_speed = [&]() {
        if (param_has(a.params, "speed") && !(need_number("speed") > 0))
            throw ValidationError("Action params.speed must be > 0");
    };
    switch (a.action_type) {
        case ActionType::STRAIGHT:
            if (need_number("distance") == 0) throw ValidationError("STRAIGHT params.distance must not be 0");
            check_speed();
            break;
        case ActionType::DOCK:
        case ActionType::PARK: {
            std::string entry = param_string(a.params, "entry", "");
            if (entry != "FORWARD" && entry != "BACKWARD")
                throw ValidationError(to_string(a.action_type) + " params.entry must be FORWARD|BACKWARD");
            if (!(need_number("undock_distance") > 0))
                throw ValidationError(to_string(a.action_type) + " params.undock_distance must be > 0");
            if (a.action_type == ActionType::DOCK) {
                double mc = need_number("min_charge");
                if (mc < 0 || mc > 100) throw ValidationError("DOCK params.min_charge must be 0~100");
            }
            check_speed();
            break;
        }
        default:
            break;
    }
}

}  // namespace

double param_double(const Params& params, const std::string& key, double def) {
    auto it = params.find(key);
    if (it == params.end()) return def;
    if (std::holds_alternative<double>(it->second)) return std::get<double>(it->second);
    if (std::holds_alternative<std::string>(it->second)) {
        try { return std::stod(std::get<std::string>(it->second)); } catch (...) { return def; }
    }
    if (std::holds_alternative<bool>(it->second)) return std::get<bool>(it->second) ? 1.0 : 0.0;
    return def;
}

std::string param_string(const Params& params, const std::string& key, const std::string& def) {
    auto it = params.find(key);
    if (it == params.end()) return def;
    if (std::holds_alternative<std::string>(it->second)) return std::get<std::string>(it->second);
    if (std::holds_alternative<double>(it->second)) {
        double d = std::get<double>(it->second);
        if (d == (long long)d) return std::to_string((long long)d);
        return std::to_string(d);
    }
    return std::get<bool>(it->second) ? "true" : "false";
}

cJSON* Action::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "action_id", action_id.c_str());
    cJSON_AddStringToObject(o, "action_type", to_string(action_type).c_str());
    cJSON_AddNumberToObject(o, "duration", duration);
    if (payload_id) cJSON_AddStringToObject(o, "payload_id", payload_id->c_str());
    else cJSON_AddNullToObject(o, "payload_id");
    cJSON_AddItemToObject(o, "params", params_to_json(params));
    return o;
}

Action Action::from_json(cJSON* obj) {
    static const std::unordered_set<std::string> known = {"action_id", "action_type", "duration",
                                                            "payload_id", "params"};
    require_no_extra_fields(obj, known, "Action");
    Action a;
    a.action_id = get_str(obj, "action_id", "Action");
    a.action_type = action_type_from(get_str(obj, "action_type", "Action"));
    a.duration = get_number_opt(obj, "duration", 0.0);
    if (a.duration < 0) throw ValidationError("Action.duration must be >= 0");
    std::string pid = get_str_opt(obj, "payload_id", "\x01");
    if (pid != "\x01") a.payload_id = pid;
    a.params = parse_params(cJSON_GetObjectItemCaseSensitive(obj, "params"), "Action");
    check_action_params(a);
    return a;
}

cJSON* TimeWindow::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "enter", enter);
    cJSON_AddNumberToObject(o, "exit", exit);
    return o;
}

TimeWindow TimeWindow::from_json(cJSON* obj) {
    static const std::unordered_set<std::string> known = {"enter", "exit"};
    require_no_extra_fields(obj, known, "TimeWindow");
    TimeWindow t;
    t.enter = get_number(obj, "enter", "TimeWindow");
    t.exit = get_number(obj, "exit", "TimeWindow");
    if (t.exit < t.enter) throw ValidationError("TimeWindow.exit cannot precede enter");
    return t;
}

cJSON* Point::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "x", x);
    cJSON_AddNumberToObject(o, "y", y);
    return o;
}

cJSON* ObstacleInfo::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "x", x);
    cJSON_AddNumberToObject(o, "y", y);
    cJSON_AddNumberToObject(o, "width", width);
    cJSON_AddNumberToObject(o, "height", height);
    return o;
}

cJSON* NodeProgress::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "node_id", node_id.c_str());
    cJSON_AddNumberToObject(o, "sequence_id", sequence_id);
    cJSON_AddBoolToObject(o, "released", released);
    return o;
}

cJSON* ActionProgress::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "action_id", action_id.c_str());
    cJSON_AddStringToObject(o, "status", to_string(status).c_str());
    return o;
}

cJSON* ErrorInfo::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "error_type", error_type.c_str());
    cJSON_AddStringToObject(o, "level", to_string(level).c_str());
    cJSON_AddStringToObject(o, "description", description.c_str());
    return o;
}

cJSON* BatteryInfo::to_json() const {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "charge", charge);
    cJSON_AddBoolToObject(o, "charging", charging);
    if (estimated_runtime) cJSON_AddNumberToObject(o, "estimated_runtime", *estimated_runtime);
    else cJSON_AddNullToObject(o, "estimated_runtime");
    return o;
}

// -- OrderNode / Order ----------------------------------------------------

OrderNode OrderNode::from_json(cJSON* obj) {
    static const std::unordered_set<std::string> known = {
        "node_id", "sequence_id", "released", "position", "action", "reserved_window", "allowed_zone_ids"};
    require_no_extra_fields(obj, known, "OrderNode");
    OrderNode n;
    n.node_id = get_str(obj, "node_id", "OrderNode");
    n.sequence_id = static_cast<int>(get_int(obj, "sequence_id", "OrderNode"));
    if (n.sequence_id < 0) throw ValidationError("OrderNode.sequence_id must be >= 0");
    n.released = get_bool(obj, "released", "OrderNode");
    n.position = pose_from_json(require_field(obj, "position", "OrderNode"));
    cJSON* act = cJSON_GetObjectItemCaseSensitive(obj, "action");
    if (act && !cJSON_IsNull(act)) n.action = Action::from_json(act);
    cJSON* win = cJSON_GetObjectItemCaseSensitive(obj, "reserved_window");
    if (win && !cJSON_IsNull(win)) n.reserved_window = TimeWindow::from_json(win);
    cJSON* zones = cJSON_GetObjectItemCaseSensitive(obj, "allowed_zone_ids");
    if (zones) {
        if (!cJSON_IsArray(zones)) throw ValidationError("allowed_zone_ids must be an array");
        cJSON* z;
        cJSON_ArrayForEach(z, zones) {
            if (!cJSON_IsString(z)) throw ValidationError("allowed_zone_ids entries must be strings");
            n.allowed_zone_ids.emplace_back(z->valuestring);
        }
    }
    return n;
}

Order Order::from_json_string(const std::string& payload) {
    cJSON* root = cJSON_Parse(payload.c_str());
    if (!root) throw ValidationError("failed to parse JSON");
    struct Guard { cJSON* p; ~Guard() { cJSON_Delete(p); } } guard{root};

    static const std::unordered_set<std::string> known = {
        "version", "header_id", "timestamp", "robot_id",
        "order_id", "order_update_id", "task_id", "nodes"};
    require_no_extra_fields(root, known, "Order");

    Order o;
    o.version = get_str_opt(root, "version", PROTOCOL_VERSION);
    o.header_id = get_int(root, "header_id", "Order");
    if (o.header_id < 0) throw ValidationError("Order.header_id must be >= 0");
    o.timestamp = get_number(root, "timestamp", "Order");
    o.robot_id = get_str(root, "robot_id", "Order");
    if (o.robot_id.empty()) throw ValidationError("Order.robot_id cannot be empty");

    o.order_id = get_str(root, "order_id", "Order");
    o.order_update_id = static_cast<int>(get_number_opt(root, "order_update_id", 0));
    if (o.order_update_id < 0) throw ValidationError("Order.order_update_id must be >= 0");
    std::string tid = get_str_opt(root, "task_id", "\x01");
    if (tid != "\x01") o.task_id = tid;

    cJSON* nodes = require_field(root, "nodes", "Order");
    if (!cJSON_IsArray(nodes) || cJSON_GetArraySize(nodes) < 1)
        throw ValidationError("Order.nodes must have at least 1 entry");
    int n = cJSON_GetArraySize(nodes);
    for (int i = 0; i < n; ++i) o.nodes.push_back(OrderNode::from_json(cJSON_GetArrayItem(nodes, i)));

    // _check_sequence: 오름차순 + 중복 없음 + released 는 앞에서부터 연속 true
    std::vector<int> seqs;
    for (auto& nd : o.nodes) seqs.push_back(nd.sequence_id);
    for (size_t i = 1; i < seqs.size(); ++i)
        if (seqs[i] < seqs[i - 1]) throw ValidationError("nodes must be in ascending sequence_id order");
    std::set<int> uniq(seqs.begin(), seqs.end());
    if (uniq.size() != seqs.size()) throw ValidationError("duplicate sequence_id");
    bool seen_false = false;
    for (auto& nd : o.nodes) {
        if (!nd.released) seen_false = true;
        else if (seen_false) throw ValidationError("released must be contiguous true from the front");
    }
    return o;
}

// -- PhysicsDebug -----------------------------------------------------

std::string PhysicsDebug::to_json_string() const {
    auto r5 = [](double v) { return std::round(v * 100000) / 100000; };
    auto pair = [&](const double* a) {
        cJSON* arr = cJSON_CreateArray();
        cJSON_AddItemToArray(arr, cJSON_CreateNumber(r5(a[0])));
        cJSON_AddItemToArray(arr, cJSON_CreateNumber(r5(a[1])));
        return arr;
    };
    cJSON* root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "header_id", (double)header_id);
    cJSON_AddNumberToObject(root, "timestamp", timestamp);
    cJSON_AddStringToObject(root, "robot_id", robot_id.c_str());
    cJSON_AddNumberToObject(root, "sim_time", r5(sim_time));
    cJSON_AddStringToObject(root, "state", state.c_str());
    cJSON_AddStringToObject(root, "sub_state", sub_state.c_str());
    cJSON_AddStringToObject(root, "action", action.c_str());
    cJSON_AddNumberToObject(root, "x", r5(x));
    cJSON_AddNumberToObject(root, "y", r5(y));
    cJSON_AddNumberToObject(root, "theta", r5(theta));
    cJSON_AddNumberToObject(root, "odom_x", r5(odom_x));
    cJSON_AddNumberToObject(root, "odom_y", r5(odom_y));
    cJSON_AddNumberToObject(root, "odom_theta", r5(odom_theta));
    cJSON_AddNumberToObject(root, "est_x", r5(est_x));
    cJSON_AddNumberToObject(root, "est_y", r5(est_y));
    cJSON_AddNumberToObject(root, "est_theta", r5(est_theta));
    cJSON_AddNumberToObject(root, "loc_rate", r5(loc_rate));
    cJSON_AddNumberToObject(root, "loc_sigma_xy", r5(loc_sigma_xy));
    cJSON_AddNumberToObject(root, "loc_sigma_theta", r5(loc_sigma_theta));
    cJSON_AddNumberToObject(root, "loc_gain", r5(loc_gain));
    cJSON_AddNumberToObject(root, "loc_dropout", r5(loc_dropout));
    cJSON_AddNumberToObject(root, "loc_corrections", (double)loc_corrections);
    cJSON_AddNumberToObject(root, "loc_dropouts", (double)loc_dropouts);
    cJSON_AddNumberToObject(root, "loc_step_xy", r5(loc_step_xy));
    cJSON_AddNumberToObject(root, "loc_step_theta", r5(loc_step_theta));
    cJSON_AddNumberToObject(root, "v_fwd", r5(v_fwd));
    cJSON_AddNumberToObject(root, "v_lat", r5(v_lat));
    cJSON_AddNumberToObject(root, "yaw_rate", r5(yaw_rate));
    cJSON_AddItemToObject(root, "wheel_w", pair(wheel_w));
    cJSON_AddItemToObject(root, "wheel_cmd", pair(wheel_cmd));
    cJSON_AddItemToObject(root, "slip", pair(slip));
    cJSON_AddNumberToObject(root, "cmd_v", r5(cmd_v));
    cJSON_AddNumberToObject(root, "cmd_w", r5(cmd_w));
    cJSON_AddNumberToObject(root, "wall_force", r5(wall_force));
    cJSON_AddBoolToObject(root, "ghost", ghost);
    cJSON_AddBoolToObject(root, "collided", collided);
    cJSON_AddBoolToObject(root, "at_rest", at_rest);
    cJSON_AddBoolToObject(root, "straight", straight);
    cJSON_AddNumberToObject(root, "friction", r5(friction));
    cJSON_AddNumberToObject(root, "mass", r5(mass));
    cJSON_AddNumberToObject(root, "wheel_radius", r5(wheel_radius));
    cJSON_AddNumberToObject(root, "track", r5(track));
    cJSON_AddNumberToObject(root, "max_wheel_torque", r5(max_wheel_torque));
    cJSON_AddNumberToObject(root, "max_velocity", r5(max_velocity));
    cJSON_AddNumberToObject(root, "max_acceleration", r5(max_acceleration));
    cJSON_AddNumberToObject(root, "path_points", path_points);
    cJSON_AddNumberToObject(root, "path_remaining", r5(path_remaining));
    if (has_goal) {
        cJSON* g = cJSON_CreateArray();
        cJSON_AddItemToArray(g, cJSON_CreateNumber(r5(goal_x)));
        cJSON_AddItemToArray(g, cJSON_CreateNumber(r5(goal_y)));
        cJSON_AddItemToObject(root, "goal", g);
    } else {
        cJSON_AddNullToObject(root, "goal");
    }
    char* txt = cJSON_PrintUnformatted(root);
    std::string result(txt);
    free(txt);
    cJSON_Delete(root);
    return result;
}

// -- State ------------------------------------------------------------

std::string State::to_json_string() const {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "version", version.c_str());
    cJSON_AddNumberToObject(root, "header_id", (double)header_id);
    cJSON_AddNumberToObject(root, "timestamp", timestamp);
    cJSON_AddStringToObject(root, "robot_id", robot_id.c_str());
    cJSON_AddBoolToObject(root, "traffic_control", traffic_control);

    cJSON_AddStringToObject(root, "state", to_string(state).c_str());
    if (sub_state) cJSON_AddStringToObject(root, "sub_state", to_string(*sub_state).c_str());
    else cJSON_AddNullToObject(root, "sub_state");
    cJSON_AddStringToObject(root, "mode", to_string(mode).c_str());
    cJSON_AddBoolToObject(root, "paused", paused);
    if (pause_source) cJSON_AddStringToObject(root, "pause_source", to_string(*pause_source).c_str());
    else cJSON_AddNullToObject(root, "pause_source");

    cJSON_AddItemToObject(root, "pose", pose.to_json());
    cJSON_AddNumberToObject(root, "velocity", velocity);
    cJSON_AddNumberToObject(root, "width", width);
    cJSON_AddNumberToObject(root, "length", length);

    cJSON* lp = cJSON_CreateArray();
    for (auto& p : local_path) cJSON_AddItemToArray(lp, p.to_json());
    cJSON_AddItemToObject(root, "local_path", lp);

    if (map_id) cJSON_AddStringToObject(root, "map_id", map_id->c_str());
    else cJSON_AddNullToObject(root, "map_id");
    if (map_version) cJSON_AddStringToObject(root, "map_version", map_version->c_str());
    else cJSON_AddNullToObject(root, "map_version");
    cJSON_AddStringToObject(root, "localization", to_string(localization).c_str());
    cJSON_AddNumberToObject(root, "localization_score", localization_score);

    cJSON_AddStringToObject(root, "last_node_id", last_node_id.c_str());
    cJSON_AddNumberToObject(root, "last_node_sequence_id", last_node_sequence_id);

    if (order_id) cJSON_AddStringToObject(root, "order_id", order_id->c_str());
    else cJSON_AddNullToObject(root, "order_id");
    cJSON_AddNumberToObject(root, "order_update_id", order_update_id);

    cJSON* ns = cJSON_CreateArray();
    for (auto& n : node_states) cJSON_AddItemToArray(ns, n.to_json());
    cJSON_AddItemToObject(root, "node_states", ns);

    cJSON* as = cJSON_CreateArray();
    for (auto& a : action_states) cJSON_AddItemToArray(as, a.to_json());
    cJSON_AddItemToObject(root, "action_states", as);

    cJSON_AddItemToObject(root, "battery", battery.to_json());

    cJSON* errs = cJSON_CreateArray();
    for (auto& e : errors) cJSON_AddItemToArray(errs, e.to_json());
    cJSON_AddItemToObject(root, "errors", errs);

    cJSON* obs = cJSON_CreateArray();
    for (auto& o : detected_obstacles) cJSON_AddItemToArray(obs, o.to_json());
    cJSON_AddItemToObject(root, "detected_obstacles", obs);

    char* txt = cJSON_PrintUnformatted(root);
    std::string result(txt);
    free(txt);
    cJSON_Delete(root);
    return result;
}

// -- Connection ---------------------------------------------------------

std::string Connection::to_json_string() const {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "version", version.c_str());
    cJSON_AddNumberToObject(root, "header_id", (double)header_id);
    cJSON_AddNumberToObject(root, "timestamp", timestamp);
    cJSON_AddStringToObject(root, "robot_id", robot_id.c_str());
    cJSON_AddStringToObject(root, "connection_state", to_string(connection_state).c_str());
    char* txt = cJSON_PrintUnformatted(root);
    std::string result(txt);
    free(txt);
    cJSON_Delete(root);
    return result;
}

// -- InstantAction --------------------------------------------------------

InstantAction InstantAction::from_json_string(const std::string& payload) {
    cJSON* root = cJSON_Parse(payload.c_str());
    if (!root) throw ValidationError("failed to parse JSON");
    struct Guard { cJSON* p; ~Guard() { cJSON_Delete(p); } } guard{root};

    static const std::unordered_set<std::string> known = {
        "version", "header_id", "timestamp", "robot_id", "action_id", "action_type", "params"};
    require_no_extra_fields(root, known, "InstantAction");

    InstantAction a;
    a.version = get_str_opt(root, "version", PROTOCOL_VERSION);
    a.header_id = ::fms::get_int(root, "header_id", "InstantAction");
    a.timestamp = get_number(root, "timestamp", "InstantAction");
    a.robot_id = get_str(root, "robot_id", "InstantAction");
    a.action_id = get_str(root, "action_id", "InstantAction");
    a.action_type = instant_action_type_from(get_str(root, "action_type", "InstantAction"));

    cJSON* params = cJSON_GetObjectItemCaseSensitive(root, "params");
    if (params && cJSON_IsObject(params)) a.params = parse_params(params, "InstantAction");
    return a;
}

std::string InstantAction::to_json_string() const {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "version", version.c_str());
    cJSON_AddNumberToObject(root, "header_id", (double)header_id);
    cJSON_AddNumberToObject(root, "timestamp", timestamp);
    cJSON_AddStringToObject(root, "robot_id", robot_id.c_str());
    cJSON_AddStringToObject(root, "action_id", action_id.c_str());
    cJSON_AddStringToObject(root, "action_type", to_string(action_type).c_str());
    cJSON_AddItemToObject(root, "params", params_to_json(params));
    char* txt = cJSON_PrintUnformatted(root);
    std::string result(txt);
    free(txt);
    cJSON_Delete(root);
    return result;
}

double InstantAction::get_double(const std::string& key, double def) const { return param_double(params, key, def); }

std::string InstantAction::get_string(const std::string& key, const std::string& def) const {
    return param_string(params, key, def);
}

int InstantAction::get_int(const std::string& key, int def) const {
    return static_cast<int>(get_double(key, (double)def));
}

bool InstantAction::has(const std::string& key) const { return param_has(params, key); }

// -- 토픽 헬퍼 ------------------------------------------------------------

std::string topic_order(const std::string& id) { return std::string(TOPIC_PREFIX) + "/" + id + "/order"; }
std::string topic_state(const std::string& id) { return std::string(TOPIC_PREFIX) + "/" + id + "/state"; }
std::string topic_connection(const std::string& id) { return std::string(TOPIC_PREFIX) + "/" + id + "/connection"; }
std::string topic_physics(const std::string& id) { return std::string(TOPIC_PREFIX) + "/" + id + "/physics"; }
std::string topic_instant(const std::string& id) { return std::string(TOPIC_PREFIX) + "/" + id + "/instant"; }
std::string topic_sim_teleport(const std::string& id) { return "sim/v1/" + id + "/teleport"; }

}  // namespace fms
