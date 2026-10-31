// GUI 스레드 -> 로봇 tick 루프 스레드로 넘기는 명령 큐.
// robot_client/gui.py 의 command_queue 패턴과 동일: GUI 는 여기 넣기만 하고,
// 실제 실행은 main.cpp 의 tick 루프(로봇 상태를 소유한 스레드)가 한다 --
// executor 를 GUI 스레드에서 직접 mutate 하지 않기 위함.
#pragma once

#include <mutex>
#include <optional>
#include <queue>
#include <string>
#include <variant>

#include "schemas.hpp"

namespace fms {

struct CmdMove { double x, y; };
struct CmdRelocalize { double x, y, theta; };    // 강제 로컬라이징 (로봇을 옮기고 바로 LOCALIZED)
struct CmdLocalizeHint { double x, y, theta; };  // 위치 힌트 -> 스캔 매칭
struct CmdPressStart {};
struct CmdPressStop {};
struct CmdEstop { bool latched; };
struct CmdMode { OperatingMode mode; };
struct CmdConfigure {
    std::string robot_id;
    double robot_width, robot_length, max_velocity, max_acceleration, sensor_range;
    double friction, mass;  // 물리: 바퀴-바닥 마찰계수, 질량 (kg)
};
struct CmdResyncMap {};  // 마지막 SET_MAP 을 다시 받는다 (다운로드 실패 복구용)
struct CmdSetBattery { double charge; };  // GUI 디버그 버튼 (100% / 1%)

using GuiCommand = std::variant<CmdMove, CmdRelocalize, CmdLocalizeHint, CmdPressStart, CmdPressStop, CmdEstop, CmdMode,
                                 CmdConfigure, CmdResyncMap, CmdSetBattery>;

class CommandQueue {
public:
    void push(GuiCommand cmd) {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.push(std::move(cmd));
    }
    std::optional<GuiCommand> pop() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.empty()) return std::nullopt;
        GuiCommand c = std::move(queue_.front());
        queue_.pop();
        return c;
    }

private:
    std::mutex mutex_;
    std::queue<GuiCommand> queue_;
};

}  // namespace fms
