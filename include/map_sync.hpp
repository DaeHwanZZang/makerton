// 맵 다운로더 (SET_MAP 수신 -> 번들 HTTP GET -> 압축해제 -> MapModel 리로드).
//
// FMS coordinator 가 로봇 등록/맵 활성화 시 fms/v1/{id}/instant 로 SET_MAP 을
// 내려보낸다 (params: map_id=map_uid, map_version, url). 번들 자체는 MQTT 로 못
// 보내므로 url 로 HTTP GET 해서 받는다.
//
// 다운로드는 느리다(네트워크+압축해제). tick 루프를 막지 않도록 백그라운드
// 워커 스레드에서 처리하고, 준비되면 tick 루프가 take_ready() 로 회수해서
// executor 의 맵을 교체한다. 실패해도 로봇은 기존 맵으로 계속 돈다.
//
// HTTP/unzip 은 `curl`/`unzip` 을 fork+exec 로 호출한다 (macOS/Ubuntu 기본 탑재,
// cmake 없는 빌드에 라이브러리 추가 안 함). shell 을 안 거치므로 url 인젝션 없음.
#pragma once

#include <condition_variable>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "map_model.hpp"

namespace fms {

enum class MapSyncState { NONE, SYNCING, SYNCED, FAILED };
std::string to_string(MapSyncState s);

class MapSync {
public:
    // cache_root: 맵을 푸는 최상위 디렉터리. 맵 하나 = cache_root/<map_uid>/.
    explicit MapSync(std::string cache_root, std::string robot_id);
    ~MapSync();

    MapSync(const MapSync&) = delete;
    MapSync& operator=(const MapSync&) = delete;

    // SET_MAP 수신 시 호출 (tick 루프 스레드). 워커에게 작업을 넘기고 즉시 반환한다.
    // 같은 (uid,version) 이 이미 SYNCED 면 아무것도 안 한다.
    void request(std::string map_uid, std::string map_version, std::string url);

    // 마지막 request 를 다시 시도한다 (GUI 의 Re-sync 버튼).
    void retry_last();

    // 워커가 새 맵을 다 받았으면 여기서 회수한다 (tick 루프 스레드, 매 tick).
    // 반환된 model 의 소유권은 호출자에게 넘어간다. executor 가 이전에 들고 있던
    // MapModel 포인터는 호출자가 이 model 로 교체하기 전까지 유효해야 한다.
    struct Ready {
        std::unique_ptr<MapModel> model;
        std::string map_uid;
        std::string map_version;
    };
    std::optional<Ready> take_ready();

    // GUI 표시용 스냅샷 (GUI 스레드).
    struct Status {
        MapSyncState state = MapSyncState::NONE;
        std::string map_uid;
        std::string map_version;
        std::string error;
    };
    Status status() const;

private:
    struct Job {
        std::string map_uid;
        std::string map_version;
        std::string url;
    };

    std::string cache_root_;
    std::string robot_id_;

    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::thread worker_;
    bool stop_ = false;

    std::optional<Job> pending_;   // 워커가 처리할 다음 작업
    std::optional<Job> last_job_;  // retry 용
    std::string synced_uid_, synced_version_;  // 마지막으로 성공한 맵

    MapSyncState state_ = MapSyncState::NONE;
    std::string status_uid_, status_version_, error_;

    std::optional<Ready> ready_;   // 워커 -> tick 루프 인수인계

    void worker_loop();
    void run_job(const Job& job);          // mutex 안 잡은 상태로 실행
    void set_status(MapSyncState st, const std::string& uid, const std::string& ver,
                    const std::string& err);

    // fork+exec 로 argv 실행, 종료코드 0 이면 true. mutex 무관.
    static bool run_process(const std::vector<std::string>& argv, std::string* err);
};

}  // namespace fms
