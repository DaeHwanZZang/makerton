#include "path_refine.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <random>

namespace fms {

namespace {

constexpr float kInf = 1e20f;

// Felzenszwalb 1D 제곱거리 변환. f: 입력(0 또는 kInf), d: 출력. n 개.
void edt_1d(const float* f, float* d, int n, int* v, float* z) {
    int k = 0;
    v[0] = 0;
    z[0] = -kInf;
    z[1] = kInf;
    for (int q = 1; q < n; ++q) {
        auto intersect = [&](int a, int b) {
            return ((f[b] + (float)b * b) - (f[a] + (float)a * a)) / (2.0f * (b - a));
        };
        float s = intersect(v[k], q);
        while (s <= z[k]) {
            --k;
            s = intersect(v[k], q);
        }
        ++k;
        v[k] = q;
        z[k] = s;
        z[k + 1] = kInf;
    }
    k = 0;
    for (int q = 0; q < n; ++q) {
        while (z[k + 1] < (float)q) ++k;
        float diff = (float)(q - v[k]);
        d[q] = diff * diff + f[v[k]];
    }
}

struct Sampler {
    const ClearanceField& field;
    double step;
    double hard_px;       // dist_px <= hard_px 이면 충돌 (planner 팽창 기준과 동일)
    double safe_px;       // dist_px < safe_px 이면 소프트 페널티
    std::pair<double, double> exempt_a, exempt_b;
    double exempt_r;

    bool exempt(double x, double y) const {
        return std::hypot(x - exempt_a.first, y - exempt_a.second) < exempt_r ||
               std::hypot(x - exempt_b.first, y - exempt_b.second) < exempt_r;
    }
};

struct Cost {
    double length = 0, turn = 0, clear = 0;
    int collisions = 0;
};

Cost evaluate(const Sampler& s, const std::vector<std::pair<double, double>>& pts) {
    Cost c;
    double res = s.field.resolution();
    for (size_t i = 0; i + 1 < pts.size(); ++i) {
        double dx = pts[i + 1].first - pts[i].first, dy = pts[i + 1].second - pts[i].second;
        double len = std::hypot(dx, dy);
        c.length += len;
        if (i + 2 < pts.size()) {
            double dx2 = pts[i + 2].first - pts[i + 1].first, dy2 = pts[i + 2].second - pts[i + 1].second;
            if (len > 1e-9 && std::hypot(dx2, dy2) > 1e-9) {
                double a = std::atan2(dy2, dx2) - std::atan2(dy, dx);
                a = std::fabs(std::remainder(a, 2 * M_PI));
                c.turn += a;
            }
        }
        int n = std::max(1, (int)std::ceil(len / s.step));
        for (int j = 1; j <= n; ++j) {
            double t = (double)j / n;
            double x = pts[i].first + dx * t, y = pts[i].second + dy * t;
            if (s.exempt(x, y)) continue;
            double d = s.field.dist_px(x, y);
            if (d <= s.hard_px) ++c.collisions;
            else if (d < s.safe_px) {
                double shortfall = (s.safe_px - d) * res;  // m
                c.clear += shortfall * shortfall * (len / n);
            }
        }
    }
    return c;
}

double total(const Cost& c) {
    constexpr double W_LEN = 1.0, W_TURN = 0.3, W_CLEAR = 20.0, W_COLL = 1e6;
    return W_LEN * c.length + W_TURN * c.turn + W_CLEAR * c.clear + W_COLL * c.collisions;
}

}  // namespace

ClearanceField::ClearanceField(const MapModel& map)
    : map_(&map), w_(map.metadata.width_px), h_(map.metadata.height_px), resolution_(map.metadata.resolution) {
    const auto& grid = map.classify_grid();
    std::vector<float> g(grid.size());
    for (size_t i = 0; i < grid.size(); ++i) g[i] = grid[i] == 2 ? kInf : 0.0f;

    int m = std::max(w_, h_);
    std::vector<float> f(m), d(m), z(m + 1);
    std::vector<int> v(m);
    for (int x = 0; x < w_; ++x) {  // 열 방향
        for (int y = 0; y < h_; ++y) f[y] = g[(size_t)y * w_ + x];
        edt_1d(f.data(), d.data(), h_, v.data(), z.data());
        for (int y = 0; y < h_; ++y) g[(size_t)y * w_ + x] = d[y];
    }
    for (int y = 0; y < h_; ++y) {  // 행 방향
        for (int x = 0; x < w_; ++x) f[x] = g[(size_t)y * w_ + x];
        edt_1d(f.data(), d.data(), w_, v.data(), z.data());
        for (int x = 0; x < w_; ++x) g[(size_t)y * w_ + x] = d[x];
    }
    dist_.resize(g.size());
    for (size_t i = 0; i < g.size(); ++i) dist_[i] = g[i] >= kInf * 0.5f ? 1e9f : std::sqrt(g[i]);
}

double ClearanceField::dist_px(double x, double y) const {
    if (!map_->in_bounds(x, y)) return 0.0;
    auto [px, py] = map_->world_to_pixel(x, y);
    if (px < 0 || px >= w_ || py < 0 || py >= h_) return 0.0;
    return dist_[(size_t)py * w_ + px];
}

std::vector<std::pair<double, double>> refine_path(
    const MapModel& map, const ClearanceField& field, std::pair<double, double> start,
    const std::vector<std::pair<double, double>>& path, double robot_width,
    const PathRefineParams& params) {
    // 움직일 수 있는 점 = 마지막(목표)을 뺀 나머지
    if (path.size() < 2) return path;
    const size_t k = path.size() - 1;
    const size_t dim = 2 * k;
    const std::pair<double, double> goal = path.back();

    double res = map.metadata.resolution;
    int radius_px = (int)std::ceil((robot_width / 2) / res);
    Sampler s{field, std::min(0.05, res), (double)radius_px, radius_px + params.safe_margin / res,
              start, goal, radius_px * res + 2 * res};

    auto build = [&](const std::vector<double>& delta) {
        std::vector<std::pair<double, double>> pts;
        pts.reserve(k + 2);
        pts.push_back(start);
        for (size_t i = 0; i < k; ++i)
            pts.emplace_back(path[i].first + delta[2 * i], path[i].second + delta[2 * i + 1]);
        pts.push_back(goal);
        return pts;
    };

    std::vector<double> zero(dim, 0.0);
    Cost base = evaluate(s, build(zero));

    std::mt19937 rng(params.seed);
    std::uniform_real_distribution<double> u01(0.0, 1.0);
    const double r = params.max_offset;
    const double vmax = 0.4 * r;
    const int n = std::max(2, params.particles);

    std::vector<std::vector<double>> X(n, std::vector<double>(dim)), V(n, std::vector<double>(dim, 0.0));
    for (int i = 0; i < n; ++i)
        for (size_t d = 0; d < dim; ++d) X[i][d] = i == 0 ? 0.0 : (u01(rng) * 2 - 1) * r;  // 0번 = A* 원본

    auto pbest = X;
    std::vector<double> pcost(n);
    for (int i = 0; i < n; ++i) pcost[i] = total(evaluate(s, build(X[i])));
    int gi = (int)(std::min_element(pcost.begin(), pcost.end()) - pcost.begin());
    std::vector<double> gbest = pbest[gi];
    double gcost = pcost[gi];

    for (int it = 0; it < params.iterations; ++it) {
        double w = 0.9 - 0.5 * it / std::max(1, params.iterations);
        for (int i = 0; i < n; ++i) {
            for (size_t d = 0; d < dim; ++d) {
                double v = w * V[i][d] + 1.5 * u01(rng) * (pbest[i][d] - X[i][d]) +
                           1.5 * u01(rng) * (gbest[d] - X[i][d]);
                V[i][d] = std::clamp(v, -vmax, vmax);
                X[i][d] = std::clamp(X[i][d] + V[i][d], -r, r);
            }
            double c = total(evaluate(s, build(X[i])));
            if (c < pcost[i]) {
                pcost[i] = c;
                pbest[i] = X[i];
                if (c < gcost) {
                    gcost = c;
                    gbest = X[i];
                }
            }
        }
    }

    auto pts = build(gbest);
    if (evaluate(s, pts).collisions > base.collisions) return path;
    return std::vector<std::pair<double, double>>(pts.begin() + 1, pts.end());
}

}  // namespace fms
