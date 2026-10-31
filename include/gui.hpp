// 로봇 제어판 (Qt Widgets). robot_client/gui.py(Tk) 의 1:1 포팅 -- 물리 패널
// (start/stop/e-stop/auto-manual) + 실시간 상태 + 설정변경(IDLE 전용) + 이동명령
// + 강제 재위치. MQTT 를 안 타고 CommandQueue 로 tick 루프 스레드에 명령을 넘긴다.
#pragma once

#include <functional>
#include <mutex>

#include "command_queue.hpp"
#include "executor.hpp"
#include "map_sync.hpp"

namespace fms {

// 블로킹 호출 -- Qt 이벤트루프를 돌리다 창이 닫히면 on_close() 를 부르고 반환한다.
// exec_mutex 는 tick 루프 스레드와 공유하는 락 (executor 동시 접근 보호).
// map_sync 는 자체 락이 있어 GUI 스레드에서 status() 를 바로 읽어도 된다.
void launch_gui(int argc, char** argv, OrderExecutor& executor, std::mutex& exec_mutex,
                 CommandQueue& queue, MapSync& map_sync, double max_velocity, double max_acceleration,
                 double robot_width, double robot_length, std::function<void()> on_close);

}  // namespace fms
