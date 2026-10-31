#include "recorder.hpp"

#include <ctime>
#include <filesystem>

#include "log.hpp"

namespace fms {

namespace {

// CSV 한 칸. 콤마/따옴표/줄바꿈이 있으면 따옴표로 감싼다
std::string cell(const std::string& s) {
    if (s.find_first_of(",\"\n") == std::string::npos) return s;
    std::string out = "\"";
    for (char c : s) {
        if (c == '"') out += '"';
        out += c;
    }
    return out + "\"";
}

}  // namespace

Recorder::Recorder(const std::string& dir, const std::string& robot_id, const std::string& meta_json) {
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    if (ec) {
        log::error("[%s] record: cannot create %s: %s", robot_id.c_str(), dir.c_str(), ec.message().c_str());
        return;
    }
    char stamp[32];
    time_t now = time(nullptr);
    struct tm tmv;
    localtime_r(&now, &tmv);
    strftime(stamp, sizeof stamp, "%Y%m%d-%H%M%S", &tmv);
    std::string base = dir + "/" + robot_id + "_" + stamp;
    csv_path_ = base + ".csv";
    csv_ = std::fopen(csv_path_.c_str(), "w");
    if (!csv_) {
        log::error("[%s] record: cannot open %s", robot_id.c_str(), csv_path_.c_str());
        return;
    }
    if (std::FILE* m = std::fopen((base + ".meta.json").c_str(), "w")) {
        std::fputs(meta_json.c_str(), m);
        std::fputc('\n', m);
        std::fclose(m);
    }
    std::fputs("t,wall_time,state,sub_state,order_id,node_index,action,"
               "x,y,theta,odom_x,odom_y,odom_theta,est_x,est_y,est_theta,v_fwd,v_lat,yaw_rate,"
               "cmd_v,cmd_w,wheel_w_l,wheel_w_r,wheel_cmd_l,wheel_cmd_r,slip_l,slip_r,"
               "wall_force,ghost,battery,charging,detected_obstacles,error\n",
               csv_);
}

Recorder::~Recorder() {
    if (csv_) std::fclose(csv_);
}

void Recorder::write(const RecordRow& r) {
    if (!csv_) return;
    const WorldTelemetry& w = r.w;
    std::fprintf(csv_, "%.3f,%.3f,%s,%s,%s,%d,%s,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f,%.4f,%.4f,%.4f,%.4f,"
                       "%.4f,%.4f,%.2f,%d,%.3f,%d,%d,%s\n",
                 r.t, r.wall_time, cell(r.state).c_str(), cell(r.sub_state).c_str(), cell(r.order_id).c_str(),
                 r.node_index, cell(r.action).c_str(), w.x, w.y, w.theta, w.odom_x, w.odom_y, w.odom_theta, w.est_x, w.est_y, w.est_theta, w.v_fwd,
                 w.v_lat, w.yaw_rate, w.cmd_v, w.cmd_w, w.wheel_w[0], w.wheel_w[1], w.wheel_cmd[0], w.wheel_cmd[1],
                 w.slip[0], w.slip[1], w.wall_force, w.ghost ? 1 : 0, r.battery, r.charging ? 1 : 0,
                 r.detected_obstacles, cell(r.error).c_str());
    if (++since_flush_ >= 20) {
        std::fflush(csv_);
        since_flush_ = 0;
    }
}

}  // namespace fms
