#include "scan_matcher.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace fms {

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr uint8_t kFree = 2;  // MapModel::classify_grid: 0=OCCUPIED, 1=UNKNOWN, 2=FREE

double normalize(double a) { return std::atan2(std::sin(a), std::cos(a)); }

// Felzenszwalb-Huttenlocher 1D 제곱 거리 변환. f 는 0(경계) 또는 kBig. 결과를 d 에 쓴다.
constexpr double kBig = 1e20;
void edt_1d(const double* f, double* d, int n, std::vector<int>& v, std::vector<double>& z) {
    int k = 0;
    v[0] = 0;
    z[0] = -std::numeric_limits<double>::infinity();
    z[1] = std::numeric_limits<double>::infinity();
    for (int q = 1; q < n; ++q) {
        double s = ((f[q] + (double)q * q) - (f[v[k]] + (double)v[k] * v[k])) / (2.0 * (q - v[k]));
        while (s <= z[k]) {
            --k;
            s = ((f[q] + (double)q * q) - (f[v[k]] + (double)v[k] * v[k])) / (2.0 * (q - v[k]));
        }
        ++k;
        v[k] = q;
        z[k] = s;
        z[k + 1] = std::numeric_limits<double>::infinity();
    }
    k = 0;
    for (int q = 0; q < n; ++q) {
        while (z[k + 1] < q) ++k;
        double dq = q - v[k];
        d[q] = dq * dq + f[v[k]];
    }
}

}  // namespace

ScanMatcher::ScanMatcher(const MapModel& map, double field_res, double sigma_hit) : map_(map), fres_(field_res) {
    const auto& m = map.metadata;
    mw_ = m.width_px;
    mh_ = m.height_px;
    res_ = m.resolution;
    ox_ = m.origin_x;
    oy_ = m.origin_y;
    const auto& g = map.classify_grid();

    fw_ = (int)std::ceil(mw_ * res_ / fres_);
    fh_ = (int)std::ceil(mh_ * res_ / fres_);
    std::vector<double> f((size_t)fw_ * fh_, kBig);

    // 벽 경계 = 4-이웃 중 FREE 가 있는 비-FREE 픽셀. 라이다가 실제로 맞는 곳이 여기다
    for (int py = 0; py < mh_; ++py) {
        for (int px = 0; px < mw_; ++px) {
            if (g[(size_t)py * mw_ + px] == kFree) continue;
            bool edge = (px > 0 && g[(size_t)py * mw_ + px - 1] == kFree) ||
                        (px + 1 < mw_ && g[(size_t)py * mw_ + px + 1] == kFree) ||
                        (py > 0 && g[(size_t)(py - 1) * mw_ + px] == kFree) ||
                        (py + 1 < mh_ && g[(size_t)(py + 1) * mw_ + px] == kFree);
            if (!edge) continue;
            int fx = std::min(fw_ - 1, (int)((px + 0.5) * res_ / fres_));
            int fy = std::min(fh_ - 1, (int)((py + 0.5) * res_ / fres_));
            f[(size_t)fy * fw_ + fx] = 0.0;
        }
    }

    // 2D 제곱 거리 변환 (열 -> 행)
    int n = std::max(fw_, fh_);
    std::vector<int> v(n);
    std::vector<double> z(n + 1), col(n), out(n);
    std::vector<double> d2((size_t)fw_ * fh_);
    for (int x = 0; x < fw_; ++x) {
        for (int y = 0; y < fh_; ++y) col[y] = f[(size_t)y * fw_ + x];
        edt_1d(col.data(), out.data(), fh_, v, z);
        for (int y = 0; y < fh_; ++y) d2[(size_t)y * fw_ + x] = out[y];
    }
    for (int y = 0; y < fh_; ++y) {
        edt_1d(&d2[(size_t)y * fw_], out.data(), fw_, v, z);
        std::copy(out.begin(), out.begin() + fw_, d2.begin() + (size_t)y * fw_);
    }

    field_.resize(d2.size());
    const double k = fres_ * fres_ / (2.0 * sigma_hit * sigma_hit);
    for (size_t i = 0; i < d2.size(); ++i) field_[i] = d2[i] >= kBig * 0.5 ? 0.0f : (float)std::exp(-d2[i] * k);
}

double ScanMatcher::field_at(double x, double y) const {
    double fx = (x - ox_) / fres_, fy = (y - oy_) / fres_;
    if (fx < 0 || fy < 0) return 0.0;
    int ix = (int)fx, iy = (int)fy;
    if (ix >= fw_ || iy >= fh_) return 0.0;
    return field_[(size_t)iy * fw_ + ix];
}

std::vector<std::pair<double, double>> ScanMatcher::simulate_scan(const Pose2& truth, const LidarParams& p,
                                                                  std::mt19937& rng) const {
    const auto& g = map_.classify_grid();
    std::normal_distribution<double> noise(0.0, p.sigma);
    std::vector<std::pair<double, double>> out;
    out.reserve(p.beams);
    // 픽셀 좌표계에서 DDA (Amanatides-Woo). 처음 만나는 비-FREE 픽셀이 반사점
    const double sx = (truth.x - ox_) / res_, sy = (truth.y - oy_) / res_;
    const double max_t = p.max_range / res_;
    for (int i = 0; i < p.beams; ++i) {
        double phi = 2.0 * kPi * i / p.beams;
        double a = truth.theta + phi;
        double dx = std::cos(a), dy = std::sin(a);
        int ix = (int)std::floor(sx), iy = (int)std::floor(sy);
        int stx = dx > 0 ? 1 : -1, sty = dy > 0 ? 1 : -1;
        double tdx = dx != 0 ? std::abs(1.0 / dx) : 1e30, tdy = dy != 0 ? std::abs(1.0 / dy) : 1e30;
        double tmx = dx != 0 ? ((dx > 0 ? (ix + 1 - sx) : (sx - ix)) * tdx) : 1e30;
        double tmy = dy != 0 ? ((dy > 0 ? (iy + 1 - sy) : (sy - iy)) * tdy) : 1e30;
        double t = 0.0;
        bool hit = false;
        while (t <= max_t) {
            if (ix < 0 || iy < 0 || ix >= mw_ || iy >= mh_) break;  // 맵 밖: 반사 없음
            if (g[(size_t)iy * mw_ + ix] != kFree) { hit = true; break; }
            if (tmx < tmy) { t = tmx; tmx += tdx; ix += stx; }
            else { t = tmy; tmy += tdy; iy += sty; }
        }
        if (!hit) continue;
        double r = t * res_ + noise(rng);
        if (r < p.min_range || r > p.max_range) continue;
        out.emplace_back(r * std::cos(phi), r * std::sin(phi));
    }
    return out;
}

double ScanMatcher::score(const std::vector<std::pair<double, double>>& scan, const Pose2& pose) const {
    if (scan.empty()) return 0.0;
    double c = std::cos(pose.theta), s = std::sin(pose.theta), sum = 0.0;
    for (auto& [rx, ry] : scan) sum += field_at(pose.x + c * rx - s * ry, pose.y + s * rx + c * ry);
    return sum / scan.size();
}

MatchResult ScanMatcher::match(const std::vector<std::pair<double, double>>& scan, const Pose2& hint,
                               const MatchParams& p) const {
    MatchResult r;
    r.pose = hint;
    r.points = (int)scan.size();
    if (r.points < p.min_points) return r;

    const double wxy2 = p.window_xy * p.window_xy, wth2 = p.window_theta * p.window_theta;
    auto prior = [&](double dx, double dy, double dth) {
        return p.prior_weight * ((dx * dx + dy * dy) / wxy2 + dth * dth / wth2);
    };

    // 한 단계 격자 탐색: 중심(cx,cy,cth) ± (rxy, rth) 를 (sxy, sth) 간격으로. 끝점은 stride 로 솎는다.
    // 점수(힌트 prior 반영) 상위 keep 개를 서로 떨어진 것끼리 남긴다
    struct Cand { double adj, x, y, th; };
    std::vector<std::pair<double, double>> rot;
    auto search = [&](double cx, double cy, double cth, double rxy, double sxy, double rth, double sth, size_t stride,
                      size_t keep) {
        int nxy = (int)std::round(rxy / sxy), nth = (int)std::round(rth / sth);
        std::vector<Cand> top;
        auto offer = [&](Cand c) {
            for (auto& o : top) {
                if (std::hypot(o.x - c.x, o.y - c.y) < 0.3 && std::abs(normalize(o.th - c.th)) < 0.1) {
                    if (c.adj > o.adj) o = c;  // 같은 봉우리: 더 높은 쪽만
                    return;
                }
            }
            top.push_back(c);
            std::sort(top.begin(), top.end(), [](const Cand& a, const Cand& b) { return a.adj > b.adj; });
            if (top.size() > keep) top.pop_back();
        };
        for (int it = -nth; it <= nth; ++it) {
            double th = cth + it * sth;
            double dth = normalize(th - hint.theta);
            if (std::abs(dth) > p.window_theta + 1e-9) continue;
            double c = std::cos(th), s = std::sin(th);
            rot.clear();
            for (size_t i = 0; i < scan.size(); i += stride) {
                auto [rx, ry] = scan[i];
                rot.emplace_back(c * rx - s * ry, s * rx + c * ry);
            }
            for (int iy = -nxy; iy <= nxy; ++iy) {
                double y = cy + iy * sxy;
                for (int ix = -nxy; ix <= nxy; ++ix) {
                    double x = cx + ix * sxy;
                    double dx = x - hint.x, dy = y - hint.y;
                    if (std::abs(dx) > p.window_xy + 1e-9 || std::abs(dy) > p.window_xy + 1e-9) continue;
                    double sum = 0.0;
                    for (auto& [ox, oy] : rot) sum += field_at(x + ox, y + oy);
                    double adj = sum / rot.size() - prior(dx, dy, dth);
                    if (top.size() < keep || adj > top.back().adj) offer(Cand{adj, x, y, th});
                }
            }
        }
        return top;
    };

    const double deg = kPi / 180.0;
    size_t coarse_stride = std::max<size_t>(1, scan.size() / 90);
    Cand best{-1e9, hint.x, hint.y, hint.theta};
    for (auto& c : search(hint.x, hint.y, hint.theta, p.window_xy, 0.1, p.window_theta, 2.0 * deg, coarse_stride, 3)) {
        auto fine = search(c.x, c.y, c.th, 0.1, 0.01, 2.0 * deg, 0.25 * deg, 1, 1);
        if (!fine.empty() && fine[0].adj > best.adj) best = fine[0];
    }
    double bx = best.x, by = best.y, bth = best.th;

    r.pose = Pose2{bx, by, normalize(bth)};
    r.score = score(scan, r.pose);
    r.ok = r.score >= p.min_score;
    return r;
}

}  // namespace fms
