#include "map_sync.hpp"

#include <cjson/cJSON.h>
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "log.hpp"

extern char** environ;

namespace fs = std::filesystem;

namespace fms {

std::string to_string(MapSyncState s) {
    switch (s) {
        case MapSyncState::NONE: return "NONE";
        case MapSyncState::SYNCING: return "SYNCING";
        case MapSyncState::SYNCED: return "SYNCED";
        case MapSyncState::FAILED: return "FAILED";
    }
    return "NONE";
}

namespace {

std::string read_file(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open " + p.string());
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

// manifest.json 에서 "version" 문자열을 읽는다.
std::string manifest_version(const fs::path& manifest_path) {
    std::string text = read_file(manifest_path);
    cJSON* root = cJSON_Parse(text.c_str());
    if (!root) throw std::runtime_error("manifest.json 파싱 실패");
    cJSON* v = cJSON_GetObjectItemCaseSensitive(root, "version");
    std::string result = (v && cJSON_IsString(v)) ? v->valuestring : "";
    cJSON_Delete(root);
    if (result.empty()) throw std::runtime_error("manifest.json 에 version 없음");
    return result;
}

}  // namespace

MapSync::MapSync(std::string cache_root, std::string robot_id)
    : cache_root_(std::move(cache_root)), robot_id_(std::move(robot_id)) {
    worker_ = std::thread(&MapSync::worker_loop, this);
}

MapSync::~MapSync() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_ = true;
    }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
}

void MapSync::request(std::string map_uid, std::string map_version, std::string url) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (map_uid == synced_uid_ && map_version == synced_version_) {
        log::debug("[%s] SET_MAP: 이미 %s@%s 동기화됨, 무시", robot_id_.c_str(), map_uid.c_str(),
                   map_version.c_str());
        return;
    }
    Job job{std::move(map_uid), std::move(map_version), std::move(url)};
    last_job_ = job;
    pending_ = std::move(job);
    cv_.notify_all();
}

void MapSync::retry_last() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!last_job_) {
        log::warning("[%s] map re-sync 요청됐지만 이전 SET_MAP 이 없다", robot_id_.c_str());
        return;
    }
    synced_uid_.clear();
    synced_version_.clear();
    pending_ = last_job_;
    cv_.notify_all();
}

std::optional<MapSync::Ready> MapSync::take_ready() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ready_) return std::nullopt;
    auto out = std::move(*ready_);
    ready_.reset();
    return out;
}

MapSync::Status MapSync::status() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return Status{state_, status_uid_, status_version_, error_};
}

void MapSync::set_status(MapSyncState st, const std::string& uid, const std::string& ver,
                         const std::string& err) {
    std::lock_guard<std::mutex> lock(mutex_);
    state_ = st;
    status_uid_ = uid;
    status_version_ = ver;
    error_ = err;
}

void MapSync::worker_loop() {
    for (;;) {
        Job job;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock, [&] { return stop_ || pending_.has_value(); });
            if (stop_) return;
            job = std::move(*pending_);
            pending_.reset();
        }
        run_job(job);
    }
}

void MapSync::run_job(const Job& job) {
    set_status(MapSyncState::SYNCING, job.map_uid, job.map_version, "");
    log::info("[%s] map sync 시작: uid=%s version=%s", robot_id_.c_str(), job.map_uid.c_str(),
              job.map_version.c_str());

    try {
        fs::create_directories(cache_root_);
        fs::path target = fs::path(cache_root_) / job.map_uid;
        fs::path manifest = target / "manifest.json";

        std::string have_version;
        if (fs::exists(manifest)) {
            try {
                have_version = manifest_version(manifest);
            } catch (...) {
                have_version.clear();
            }
        }

        if (have_version != job.map_version) {
            // 다운로드 필요.
            fs::path zip = fs::path(cache_root_) / (".dl-" + job.map_uid + ".zip");
            fs::path stage = fs::path(cache_root_) / (".stage-" + job.map_uid);
            std::error_code ec;
            fs::remove_all(stage, ec);
            fs::remove(zip, ec);

            std::string err;
            if (!run_process({"curl", "-fsS", "--max-time", "60", "-o", zip.string(), job.url}, &err))
                throw std::runtime_error("번들 다운로드 실패 (" + err + "): " + job.url);

            fs::create_directories(stage);
            if (!run_process({"unzip", "-oq", zip.string(), "-d", stage.string()}, &err))
                throw std::runtime_error("번들 압축해제 실패 (" + err + ")");
            fs::remove(zip, ec);

            if (!fs::exists(stage / "fms_map.json") || !fs::exists(stage / "manifest.json"))
                throw std::runtime_error("번들에 fms_map.json / manifest.json 이 없다");

            std::string got = manifest_version(stage / "manifest.json");
            if (got != job.map_version)
                log::warning("[%s] SET_MAP version=%s 인데 번들 manifest 는 %s -- 번들 값을 따른다",
                             robot_id_.c_str(), job.map_version.c_str(), got.c_str());

            fs::remove_all(target, ec);
            fs::create_directories(target.parent_path());
            fs::rename(stage, target, ec);
            if (ec) {  // 다른 파일시스템 등으로 rename 실패 시 복사 폴백
                fs::copy(stage, target, fs::copy_options::recursive);
                fs::remove_all(stage, ec);
            }
        } else {
            log::info("[%s] map %s@%s 이미 캐시에 있음 -- 다운로드 생략", robot_id_.c_str(),
                      job.map_uid.c_str(), job.map_version.c_str());
        }

        std::string real_version = manifest_version(target / "manifest.json");

        // 무거운 로드(PNG 디코드+분류)는 여기 워커 스레드에서 끝내둔다.
        auto model = std::make_unique<MapModel>(MapModel::load((target / "fms_map.json").string()));
        model->classify_grid();  // PNG 크기/디코드 검증 -- 깨졌으면 여기서 throw

        {
            std::lock_guard<std::mutex> lock(mutex_);
            synced_uid_ = job.map_uid;
            synced_version_ = real_version;
            ready_ = Ready{std::move(model), job.map_uid, real_version};
        }
        set_status(MapSyncState::SYNCED, job.map_uid, real_version, "");
        log::info("[%s] map sync 완료: uid=%s version=%s", robot_id_.c_str(), job.map_uid.c_str(),
                  real_version.c_str());
    } catch (const std::exception& e) {
        set_status(MapSyncState::FAILED, job.map_uid, job.map_version, e.what());
        log::error("[%s] map sync 실패: %s", robot_id_.c_str(), e.what());
    }
}

bool MapSync::run_process(const std::vector<std::string>& argv, std::string* err) {
    std::vector<char*> cargv;
    cargv.reserve(argv.size() + 1);
    for (auto& s : argv) cargv.push_back(const_cast<char*>(s.c_str()));
    cargv.push_back(nullptr);

    pid_t pid = 0;
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    // stdout 은 조용히, stderr 는 그대로 흘려보낸다 (curl -fsS 가 에러만 찍음).
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0);

    int rc = posix_spawnp(&pid, cargv[0], &actions, nullptr, cargv.data(), environ);
    posix_spawn_file_actions_destroy(&actions);
    if (rc != 0) {
        if (err) *err = std::string(cargv[0]) + " 실행 불가: " + std::strerror(rc);
        return false;
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        if (err) *err = "waitpid 실패";
        return false;
    }
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) return true;
    if (err) {
        if (WIFEXITED(status))
            *err = std::string(cargv[0]) + " 종료코드 " + std::to_string(WEXITSTATUS(status));
        else
            *err = std::string(cargv[0]) + " 비정상 종료";
    }
    return false;
}

}  // namespace fms
