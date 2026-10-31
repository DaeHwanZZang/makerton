#include "physics_world.hpp"

#include <mujoco/mujoco.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <sstream>
#include <stdexcept>
#include <unordered_map>
#include <vector>

#include "log.hpp"

namespace fms {

namespace {

constexpr double kTimestep = 0.002;        // MuJoCo 적분 간격 (s). tick 50 ms 안에서 25 번 돈다
constexpr double kWallHeight = 0.5;        // 벽 상자 높이 (m)
constexpr double kBodySkin = 0.04;         // 충돌 원판 반지름을 폭/2 보다 이만큼 줄인다 (planner 팽창이 픽셀 단위라 최악 ~1.5 cm 가까이 갈 수 있다)
constexpr double kCasterRadius = 0.04;
constexpr double kBucket = 4.0;            // 벽 공간 색인 버킷 크기 (m)
constexpr double kWindowHalf = 8.0;        // 로봇 주변 이 반경 안의 벽만 모델에 넣는다 (m)
constexpr double kRebuildDist = 3.0;       // 창 중심에서 이만큼 벗어나면 창을 다시 잡는다 (m)
constexpr double kCollisionForce = 20.0;   // 이 수직력(N) 이상으로
constexpr double kCollisionTime = 0.25;    // 이 시간(s) 이상 벽에 눌리면 충돌로 본다
constexpr double kRestSpeed = 0.02;        // 정지 판정 (m/s)
constexpr double kRestYaw = 0.05;          // 정지 판정 (rad/s)
constexpr double kServoGain = 6.0;         // 바퀴 속도 서보 게인 (N·m·s/rad). 크면 접촉과 얽혀 정지 후에도 떨린다 (60 에서 확인)

constexpr double kPi = 3.14159265358979323846;

struct Rect {
    double x0, y0, x1, y1;
};

double normalize_angle(double a) {
    while (a > kPi) a -= 2 * kPi;
    while (a < -kPi) a += 2 * kPi;
    return a;
}

// FREE 가 아닌 칸을 탐욕적으로 직사각형 병합. 같은 행의 연속 구간을 잡고 아래로 늘린다.
std::vector<Rect> merge_blocked_cells(const MapModel& map) {
    const auto& grid = map.classify_grid();  // 0=OCCUPIED 1=UNKNOWN 2=FREE
    const int w = map.metadata.width_px, h = map.metadata.height_px;
    const double res = map.metadata.resolution, ox = map.metadata.origin_x, oy = map.metadata.origin_y;
    std::vector<uint8_t> used((size_t)w * h, 0);
    auto open = [&](int r, int c) { return grid[(size_t)r * w + c] != 2 && !used[(size_t)r * w + c]; };

    std::vector<Rect> rects;
    for (int r = 0; r < h; ++r) {
        int c = 0;
        while (c < w) {
            if (!open(r, c)) { ++c; continue; }
            int c1 = c;
            while (c1 < w && open(r, c1)) ++c1;
            int r1 = r + 1;
            while (r1 < h) {
                bool all = true;
                for (int cc = c; cc < c1; ++cc)
                    if (!open(r1, cc)) { all = false; break; }
                if (!all) break;
                ++r1;
            }
            for (int rr = r; rr < r1; ++rr)
                std::fill(used.begin() + (size_t)rr * w + c, used.begin() + (size_t)rr * w + c1, 1);
            rects.push_back({ox + c * res, oy + r * res, ox + c1 * res, oy + r1 * res});
            c = c1;
        }
    }
    return rects;
}

// 벽 상자 + 공간 색인. 맵 밖(OCCUPIED 취급)은 이미지 둘레의 큰 상자 4개로 막는다.
class WallIndex {
public:
    void build(const MapModel& map) {
        rects_ = merge_blocked_cells(map);
        const auto& md = map.metadata;
        ox_ = md.origin_x;
        oy_ = md.origin_y;
        nbx_ = std::max(1, (int)std::ceil(md.width_m() / kBucket));
        nby_ = std::max(1, (int)std::ceil(md.height_m() / kBucket));
        buckets_.clear();
        always_.clear();
        for (size_t i = 0; i < rects_.size(); ++i) {
            int bx0, by0, bx1, by1;
            bucket_range(rects_[i], bx0, by0, bx1, by1);
            for (int by = by0; by <= by1; ++by)
                for (int bx = bx0; bx <= bx1; ++bx) buckets_[key(bx, by)].push_back((int)i);
        }
        // 맵 밖
        const double x0 = md.origin_x, y0 = md.origin_y, x1 = x0 + md.width_m(), y1 = y0 + md.height_m();
        const double big = 200.0;
        outer_ = {{x0 - big, y0 - big, x0, y1 + big},
                  {x1, y0 - big, x1 + big, y1 + big},
                  {x0, y0 - big, x1, y0},
                  {x0, y1, x1, y1 + big}};
        stamp_.assign(rects_.size(), 0);
        stamp_now_ = 0;
    }

    size_t size() const { return rects_.size(); }

    // AABB 와 겹치는 상자
    void query(double ax0, double ay0, double ax1, double ay1, std::vector<Rect>& out) const {
        out.clear();
        auto overlaps = [&](const Rect& r) { return r.x1 > ax0 && r.x0 < ax1 && r.y1 > ay0 && r.y0 < ay1; };
        for (const Rect& r : outer_)
            if (overlaps(r)) out.push_back(r);
        if (rects_.empty()) return;
        int bx0 = clampi((int)std::floor((ax0 - ox_) / kBucket), nbx_);
        int bx1 = clampi((int)std::floor((ax1 - ox_) / kBucket), nbx_);
        int by0 = clampi((int)std::floor((ay0 - oy_) / kBucket), nby_);
        int by1 = clampi((int)std::floor((ay1 - oy_) / kBucket), nby_);
        ++stamp_now_;
        for (int by = by0; by <= by1; ++by)
            for (int bx = bx0; bx <= bx1; ++bx) {
                auto it = buckets_.find(key(bx, by));
                if (it == buckets_.end()) continue;
                for (int id : it->second) {
                    if (stamp_[id] == stamp_now_) continue;
                    stamp_[id] = stamp_now_;
                    if (overlaps(rects_[id])) out.push_back(rects_[id]);
                }
            }
    }

private:
    std::vector<Rect> rects_, outer_;
    std::unordered_map<int64_t, std::vector<int>> buckets_;
    std::vector<int> always_;
    double ox_ = 0, oy_ = 0;
    int nbx_ = 1, nby_ = 1;
    mutable std::vector<uint32_t> stamp_;
    mutable uint32_t stamp_now_ = 0;

    static int clampi(int v, int n) { return std::max(0, std::min(n - 1, v)); }
    static int64_t key(int bx, int by) { return ((int64_t)bx << 32) | (uint32_t)by; }
    void bucket_range(const Rect& r, int& bx0, int& by0, int& bx1, int& by1) const {
        bx0 = clampi((int)std::floor((r.x0 - ox_) / kBucket), nbx_);
        bx1 = clampi((int)std::floor((r.x1 - ox_) / kBucket), nbx_);
        by0 = clampi((int)std::floor((r.y0 - oy_) / kBucket), nby_);
        by1 = clampi((int)std::floor((r.y1 - oy_) / kBucket), nby_);
    }
};

double track_of(const RobotSpec& s) { return std::max(0.2, s.width - 2 * s.wheel_radius); }

std::string make_xml(const RobotSpec& s, double x, double y, double theta, const std::vector<Rect>& walls) {
    std::ostringstream o;
    o.precision(9);
    const double R = s.wheel_radius, B = track_of(s);
    const double hl = s.length / 2;
    const double body_r = std::max(0.05, s.width / 2 - kBodySkin);  // 충돌 원판 반지름
    const double caster_x = std::max(0.05, hl - 0.07);
    const double caster_z = -(R - kCasterRadius);
    o << "<mujoco>\n"
      << "  <option timestep=\"" << kTimestep << "\" integrator=\"implicitfast\"/>\n"
      << "  <worldbody>\n"
      << "    <geom name=\"floor\" type=\"plane\" size=\"0 0 1\" friction=\"0.01 0.005 0.0001\" contype=\"1\" conaffinity=\"4\"/>\n";
    for (const Rect& r : walls) {
        double hx = (r.x1 - r.x0) / 2, hy = (r.y1 - r.y0) / 2;
        o << "    <geom type=\"box\" size=\"" << hx << " " << hy << " " << kWallHeight / 2 << "\" pos=\""
          << r.x0 + hx << " " << r.y0 + hy << " " << kWallHeight / 2 << "\" contype=\"1\" conaffinity=\"2\"/>\n";
    }
    o << "    <body name=\"base\" pos=\"" << x << " " << y << " " << R << "\" quat=\"" << std::cos(theta / 2)
      << " 0 0 " << std::sin(theta / 2) << "\">\n"
      << "      <freejoint name=\"root\"/>\n"
      // 질량/관성은 길이×폭×0.2 m 직육면체 기준 (충돌 형상과 별개). 충돌은 planner 와 같은 원판 가정이다.
      << "      <inertial pos=\"0 0 0.1\" mass=\"" << s.mass << "\" diaginertia=\"" << s.mass / 12 * (s.width * s.width + 0.04) << " "
      << s.mass / 12 * (s.length * s.length + 0.04) << " " << s.mass / 12 * (s.length * s.length + s.width * s.width) << "\"/>\n"
      << "      <geom name=\"chassis\" type=\"cylinder\" size=\"" << body_r << " 0.1\" pos=\"0 0 0.1\" mass=\"0\" contype=\"2\" conaffinity=\"1\"/>\n"
      << "      <body name=\"wl\" pos=\"0 " << B / 2 << " 0\">\n"
      << "        <joint name=\"jl\" type=\"hinge\" axis=\"0 1 0\" damping=\"0.05\"/>\n"
      << "        <geom name=\"gl\" type=\"sphere\" size=\"" << R << "\" mass=\"2\" friction=\"" << s.friction
      << " 0.005 0.0001\" contype=\"4\" conaffinity=\"0\"/>\n"
      << "      </body>\n"
      << "      <body name=\"wr\" pos=\"0 " << -B / 2 << " 0\">\n"
      << "        <joint name=\"jr\" type=\"hinge\" axis=\"0 1 0\" damping=\"0.05\"/>\n"
      << "        <geom name=\"gr\" type=\"sphere\" size=\"" << R << "\" mass=\"2\" friction=\"" << s.friction
      << " 0.005 0.0001\" contype=\"4\" conaffinity=\"0\"/>\n"
      << "      </body>\n";
    for (double cx : {caster_x, -caster_x})
        o << "      <geom type=\"sphere\" size=\"" << kCasterRadius << "\" pos=\"" << cx << " 0 " << caster_z
          << "\" condim=\"1\" priority=\"1\" friction=\"0 0 0\" mass=\"0.5\" contype=\"4\" conaffinity=\"0\"/>\n";
    o << "    </body>\n"
      << "  </worldbody>\n"
      << "  <actuator>\n"
      << "    <velocity name=\"ml\" joint=\"jl\" kv=\"" << kServoGain << "\" forcerange=\"-" << s.max_wheel_torque << " "
      << s.max_wheel_torque << "\"/>\n"
      << "    <velocity name=\"mr\" joint=\"jr\" kv=\"" << kServoGain << "\" forcerange=\"-" << s.max_wheel_torque << " "
      << s.max_wheel_torque << "\"/>\n"
      << "  </actuator>\n"
      << "</mujoco>\n";
    return o.str();
}

}  // namespace

struct PhysicsWorld::Impl {
    RobotSpec spec;
    const MapModel* map = nullptr;
    WallIndex walls;
    bool have_walls = false;

    mjModel* m = nullptr;
    mjData* d = nullptr;
    double win_cx = 0, win_cy = 0;
    bool have_window = false;

    int chassis_geom = -1, wheel_geom[2] = {-1, -1};
    int jl_qpos = 0, jr_qpos = 0, jl_dof = 0, jr_dof = 0;

    double debt = 0.0;
    double cmd_v = 0, cmd_w = 0;
    double odom_x = 0, odom_y = 0, odom_th = 0;
    Localizer loc;  // 위치추정 보정 (참 자세로 추정을 당긴다)
    double prev_al = 0, prev_ar = 0;
    double wall_force = 0, contact_time = 0;
    bool ghost = false;

    ~Impl() {
        if (d) mj_deleteData(d);
        if (m) mj_deleteModel(m);
    }

    double yaw() const {
        const mjtNum* q = d->qpos + 3;
        return std::atan2(2 * (q[0] * q[3] + q[1] * q[2]), 1 - 2 * (q[2] * q[2] + q[3] * q[3]));
    }

    void rebuild(double cx, double cy) {
        std::vector<mjtNum> qpos, qvel, ctrl;
        double ptime = 0;
        if (d) {
            qpos.assign(d->qpos, d->qpos + m->nq);
            qvel.assign(d->qvel, d->qvel + m->nv);
            ctrl.assign(d->ctrl, d->ctrl + m->nu);
            ptime = d->time;
        }
        std::vector<Rect> near;
        if (have_walls)
            walls.query(cx - kWindowHalf, cy - kWindowHalf, cx + kWindowHalf, cy + kWindowHalf, near);
        double px = qpos.empty() ? cx : qpos[0], py = qpos.empty() ? cy : qpos[1];
        double pth = d ? yaw() : 0.0;
        std::string xml = make_xml(spec, px, py, pth, near);
        // 디버깅: AMR_PHYSICS_DUMP=파일 이면 마지막으로 컴파일한 MJCF 를 그 파일에 쓴다 (파이썬 mujoco 로 열어 볼 수 있다)
        if (const char* dump = std::getenv("AMR_PHYSICS_DUMP")) {
            if (std::FILE* f = std::fopen(dump, "w")) { std::fputs(xml.c_str(), f); std::fclose(f); }
        }

        char err[1000] = "";
        mjSpec* ms = mj_parseXMLString(xml.c_str(), nullptr, err, sizeof err);
        if (!ms) throw std::runtime_error(std::string("MuJoCo XML 오류: ") + err);
        mjModel* nm = mj_compile(ms, nullptr);
        if (!nm) {
            std::string msg = mjs_getError(ms);
            mj_deleteSpec(ms);
            throw std::runtime_error("MuJoCo 컴파일 오류: " + msg);
        }
        mj_deleteSpec(ms);
        mjData* nd = mj_makeData(nm);

        if (d) mj_deleteData(d);
        if (m) mj_deleteModel(m);
        m = nm;
        d = nd;
        if (!qpos.empty() && (int)qpos.size() == m->nq && (int)qvel.size() == m->nv) {
            std::copy(qpos.begin(), qpos.end(), d->qpos);
            std::copy(qvel.begin(), qvel.end(), d->qvel);
            std::copy(ctrl.begin(), ctrl.end(), d->ctrl);
            d->time = ptime;
        }
        chassis_geom = mj_name2id(m, mjOBJ_GEOM, "chassis");
        wheel_geom[0] = mj_name2id(m, mjOBJ_GEOM, "gl");
        wheel_geom[1] = mj_name2id(m, mjOBJ_GEOM, "gr");
        jl_qpos = m->jnt_qposadr[mj_name2id(m, mjOBJ_JOINT, "jl")];
        jr_qpos = m->jnt_qposadr[mj_name2id(m, mjOBJ_JOINT, "jr")];
        jl_dof = m->jnt_dofadr[mj_name2id(m, mjOBJ_JOINT, "jl")];
        jr_dof = m->jnt_dofadr[mj_name2id(m, mjOBJ_JOINT, "jr")];
        apply_ghost();
        mj_forward(m, d);
        prev_al = d->qpos[jl_qpos];
        prev_ar = d->qpos[jr_qpos];
        win_cx = cx;
        win_cy = cy;
        have_window = true;
    }

    // 몸체 원판이 벽(비-FREE 칸, 맵 밖)에 겹치는가
    bool footprint_blocked(double x, double y, double /*th*/) const {
        if (map == nullptr) return false;
        const auto& grid = map->classify_grid();
        const auto& md = map->metadata;
        const double res = md.resolution;
        const double r = std::max(0.05, spec.width / 2 - kBodySkin);
        int px0 = (int)std::floor((x - r - md.origin_x) / res), px1 = (int)std::floor((x + r - md.origin_x) / res);
        int py0 = (int)std::floor((y - r - md.origin_y) / res), py1 = (int)std::floor((y + r - md.origin_y) / res);
        for (int py = py0; py <= py1; ++py)
            for (int px = px0; px <= px1; ++px) {
                bool outside = px < 0 || py < 0 || px >= md.width_px || py >= md.height_px;
                if (!outside && grid[(size_t)py * md.width_px + px] == 2) continue;
                double cx = md.origin_x + (px + 0.5) * res - x, cy = md.origin_y + (py + 0.5) * res - y;
                double ex = std::max(std::abs(cx) - res / 2, 0.0), ey = std::max(std::abs(cy) - res / 2, 0.0);
                if (std::hypot(ex, ey) < r) return true;
            }
        return false;
    }

    // 시작/이동 위치가 벽 안이면 몸체-벽 충돌을 끈다 (엔진이 겹침을 풀면서 로봇을 튕겨내는 걸 막는다).
    // v1.3 의 "벽 위에서도 일단 뜬다" 동작을 유지하기 위한 것.
    void apply_ghost() {
        ghost = footprint_blocked(d->qpos[0], d->qpos[1], yaw());
        m->geom_contype[chassis_geom] = ghost ? 0 : 2;
        m->geom_conaffinity[chassis_geom] = ghost ? 0 : 1;
    }

    void substep_bookkeeping() {
        double f_max = 0.0;
        for (int i = 0; i < d->ncon; ++i) {
            const mjContact& c = d->contact[i];
            if (c.geom1 != chassis_geom && c.geom2 != chassis_geom) continue;
            mjtNum f[6];
            mj_contactForce(m, d, i, f);
            f_max = std::max(f_max, (double)f[0]);
        }
        wall_force = std::max(wall_force, f_max);
        if (f_max >= kCollisionForce) contact_time += kTimestep;
        else contact_time = 0.0;

        // 바퀴 엔코더 적분 (슬립은 여기 안 보인다)
        double al = d->qpos[jl_qpos], ar = d->qpos[jr_qpos];
        double dl = (al - prev_al) * spec.wheel_radius, dr = (ar - prev_ar) * spec.wheel_radius;
        prev_al = al;
        prev_ar = ar;
        double ds = (dl + dr) / 2, dth = (dr - dl) / track_of(spec);
        odom_x += ds * std::cos(odom_th + dth / 2);
        odom_y += ds * std::sin(odom_th + dth / 2);
        odom_th = normalize_angle(odom_th + dth);
    }

    void set_wheels(double v, double w) {
        cmd_v = v;
        cmd_w = w;
        const double B = track_of(spec), R = spec.wheel_radius;
        d->ctrl[0] = (v - w * B / 2) / R;
        d->ctrl[1] = (v + w * B / 2) / R;
    }
};

PhysicsWorld::PhysicsWorld(const RobotSpec& spec, double x, double y, double theta) : impl_(std::make_unique<Impl>()) {
    impl_->spec = spec;
    impl_->rebuild(x, y);
    teleport(x, y, theta);
}

PhysicsWorld::~PhysicsWorld() = default;

void PhysicsWorld::set_map(const MapModel* map) {
    Impl& s = *impl_;
    s.map = map;
    s.have_walls = map != nullptr;
    if (map) {
        auto t0 = std::chrono::steady_clock::now();
        s.walls.build(*map);
        double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
        log::info("physics: wall boxes built from map: %zu boxes (%.0f ms)", s.walls.size(), ms);
    }
    s.rebuild(s.d->qpos[0], s.d->qpos[1]);
    s.contact_time = 0.0;
}

void PhysicsWorld::set_spec(const RobotSpec& spec) {
    Impl& s = *impl_;
    s.spec = spec;
    s.rebuild(s.d->qpos[0], s.d->qpos[1]);
    s.set_wheels(s.cmd_v, s.cmd_w);
}

void PhysicsWorld::set_friction(double mu) {
    Impl& s = *impl_;
    s.spec.friction = mu;
    for (int g : s.wheel_geom) s.m->geom_friction[3 * g] = mu;
}

void PhysicsWorld::teleport(double x, double y, double theta) {
    Impl& s = *impl_;
    if (!s.have_window || std::hypot(x - s.win_cx, y - s.win_cy) > kRebuildDist) {
        s.d->qpos[0] = x;  // rebuild 가 이 위치를 중심으로 창을 잡는다
        s.d->qpos[1] = y;
        s.rebuild(x, y);
    }
    mjData* d = s.d;
    d->qpos[0] = x;
    d->qpos[1] = y;
    d->qpos[2] = s.spec.wheel_radius;
    d->qpos[3] = std::cos(theta / 2);
    d->qpos[4] = 0;
    d->qpos[5] = 0;
    d->qpos[6] = std::sin(theta / 2);
    std::fill(d->qvel, d->qvel + s.m->nv, 0.0);
    std::fill(d->qacc_warmstart, d->qacc_warmstart + s.m->nv, 0.0);
    s.cmd_v = s.cmd_w = 0.0;
    std::fill(d->ctrl, d->ctrl + s.m->nu, 0.0);
    s.apply_ghost();
    mj_forward(s.m, d);
    s.prev_al = d->qpos[s.jl_qpos];
    s.prev_ar = d->qpos[s.jr_qpos];
    s.odom_x = x;
    s.odom_y = y;
    s.odom_th = normalize_angle(theta);
    s.loc.reset();  // 위치를 재지정하면 추정도 그 자세에서 새로 시작한다
    s.contact_time = 0.0;
    s.wall_force = 0.0;
    s.debt = 0.0;
}

void PhysicsWorld::stop() {
    Impl& s = *impl_;
    std::fill(s.d->qvel, s.d->qvel + s.m->nv, 0.0);
    s.set_wheels(0.0, 0.0);
    s.contact_time = 0.0;
    mj_forward(s.m, s.d);
}

void PhysicsWorld::drive(double v, double w) { impl_->set_wheels(v, w); }

void PhysicsWorld::step(double dt) {
    Impl& s = *impl_;
    s.debt += dt;
    int n = (int)(s.debt / kTimestep + 1e-9);
    s.debt -= n * kTimestep;
    n = std::min(n, 500);
    s.wall_force = 0.0;
    for (int i = 0; i < n; ++i) {
        mj_step(s.m, s.d);
        s.substep_bookkeeping();
    }
    // 라이다 위치추정 보정: 참 자세(+노이즈)로 추정을 당긴다
    s.loc.update(dt, Pose2{s.odom_x, s.odom_y, s.odom_th}, Pose2{s.d->qpos[0], s.d->qpos[1], s.yaw()});
    if (s.have_walls && std::hypot(s.d->qpos[0] - s.win_cx, s.d->qpos[1] - s.win_cy) > kRebuildDist)
        s.rebuild(s.d->qpos[0], s.d->qpos[1]);
}

double PhysicsWorld::x() const { return impl_->d->qpos[0]; }
double PhysicsWorld::y() const { return impl_->d->qpos[1]; }
double PhysicsWorld::theta() const { return impl_->yaw(); }
double PhysicsWorld::odom_x() const { return impl_->odom_x; }
double PhysicsWorld::odom_y() const { return impl_->odom_y; }
double PhysicsWorld::odom_theta() const { return impl_->odom_th; }
double PhysicsWorld::est_x() const {
    return impl_->loc.estimate(Pose2{impl_->odom_x, impl_->odom_y, impl_->odom_th}).x;
}
double PhysicsWorld::est_y() const {
    return impl_->loc.estimate(Pose2{impl_->odom_x, impl_->odom_y, impl_->odom_th}).y;
}
double PhysicsWorld::est_theta() const {
    return impl_->loc.estimate(Pose2{impl_->odom_x, impl_->odom_y, impl_->odom_th}).theta;
}
void PhysicsWorld::set_localization(const LocalizerParams& params) {
    impl_->loc.set_params(params);
}
void PhysicsWorld::set_estimate(double x, double y, double theta) {
    Impl& s = *impl_;
    s.loc.set_estimate(Pose2{s.odom_x, s.odom_y, s.odom_th}, Pose2{x, y, theta});
}
void PhysicsWorld::set_tracking(bool on) { impl_->loc.set_tracking(on); }
bool PhysicsWorld::tracking() const { return impl_->loc.tracking(); }
const LocalizerParams& PhysicsWorld::localization() const { return impl_->loc.params(); }
const LocalizerStats& PhysicsWorld::loc_stats() const { return impl_->loc.stats(); }

double PhysicsWorld::v_fwd() const {
    const Impl& s = *impl_;
    double th = s.yaw();
    return s.d->qvel[0] * std::cos(th) + s.d->qvel[1] * std::sin(th);
}

double PhysicsWorld::yaw_rate() const { return impl_->d->qvel[5]; }

bool PhysicsWorld::at_rest() const {
    const Impl& s = *impl_;
    double th = s.yaw();
    double vl = -s.d->qvel[0] * std::sin(th) + s.d->qvel[1] * std::cos(th);
    return std::abs(v_fwd()) < kRestSpeed && std::abs(vl) < kRestSpeed && std::abs(yaw_rate()) < kRestYaw;
}

bool PhysicsWorld::collided() const { return !impl_->ghost && impl_->contact_time >= kCollisionTime; }
bool PhysicsWorld::ghost() const { return impl_->ghost; }
double PhysicsWorld::wall_force() const { return impl_->wall_force; }
const RobotSpec& PhysicsWorld::spec() const { return impl_->spec; }
double PhysicsWorld::track() const { return track_of(impl_->spec); }

WorldTelemetry PhysicsWorld::telemetry() const {
    const Impl& s = *impl_;
    WorldTelemetry t;
    t.x = x();
    t.y = y();
    t.theta = theta();
    t.odom_x = s.odom_x;
    t.odom_y = s.odom_y;
    t.odom_theta = s.odom_th;
    Pose2 est = s.loc.estimate(Pose2{s.odom_x, s.odom_y, s.odom_th});
    t.est_x = est.x;
    t.est_y = est.y;
    t.est_theta = est.theta;
    t.loc_corrections = s.loc.stats().corrections;
    t.loc_dropouts = s.loc.stats().dropouts;
    t.loc_step_xy = s.loc.stats().last_step_xy;
    t.loc_step_theta = s.loc.stats().last_step_theta;
    double th = t.theta;
    t.v_fwd = v_fwd();
    t.v_lat = -s.d->qvel[0] * std::sin(th) + s.d->qvel[1] * std::cos(th);
    t.yaw_rate = yaw_rate();
    const double R = s.spec.wheel_radius, B = track_of(s.spec);
    t.wheel_w[0] = s.d->qvel[s.jl_dof];
    t.wheel_w[1] = s.d->qvel[s.jr_dof];
    t.wheel_cmd[0] = s.d->ctrl[0];
    t.wheel_cmd[1] = s.d->ctrl[1];
    const double ground[2] = {t.v_fwd - t.yaw_rate * B / 2, t.v_fwd + t.yaw_rate * B / 2};
    for (int i = 0; i < 2; ++i) {
        double wl = t.wheel_w[i] * R;
        t.slip[i] = (wl - ground[i]) / std::max({std::abs(wl), std::abs(ground[i]), 0.05});
    }
    t.cmd_v = s.cmd_v;
    t.cmd_w = s.cmd_w;
    t.wall_force = s.wall_force;
    t.ghost = s.ghost;
    return t;
}

}  // namespace fms
