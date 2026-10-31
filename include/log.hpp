// 아주 단순한 로거. robot_client/main.py 의 logging.basicConfig 포맷을 흉내낸다:
// "HH:MM:SS LEVEL   메시지"
#pragma once

#include <cstdarg>
#include <cstdio>
#include <ctime>
#include <string>

namespace fms::log {

enum class Level { DEBUG = 0, INFO = 1, WARNING = 2, ERROR = 3 };
inline Level& current_level() { static Level lvl = Level::INFO; return lvl; }

inline void set_level(Level l) { current_level() = l; }

inline void set_level(const std::string& s) {
    if (s == "DEBUG") current_level() = Level::DEBUG;
    else if (s == "WARNING") current_level() = Level::WARNING;
    else if (s == "ERROR") current_level() = Level::ERROR;
    else current_level() = Level::INFO;
}

inline void emit(Level lvl, const char* tag, const char* fmt, va_list args) {
    if (lvl < current_level()) return;
    time_t t = time(nullptr);
    struct tm tmv;
    localtime_r(&t, &tmv);
    char timebuf[16];
    strftime(timebuf, sizeof(timebuf), "%H:%M:%S", &tmv);
    fprintf(stderr, "%s %-7s ", timebuf, tag);
    vfprintf(stderr, fmt, args);
    fprintf(stderr, "\n");
}

inline void debug(const char* fmt, ...) {
    va_list args; va_start(args, fmt); emit(Level::DEBUG, "DEBUG", fmt, args); va_end(args);
}
inline void info(const char* fmt, ...) {
    va_list args; va_start(args, fmt); emit(Level::INFO, "INFO", fmt, args); va_end(args);
}
inline void warning(const char* fmt, ...) {
    va_list args; va_start(args, fmt); emit(Level::WARNING, "WARNING", fmt, args); va_end(args);
}
inline void error(const char* fmt, ...) {
    va_list args; va_start(args, fmt); emit(Level::ERROR, "ERROR", fmt, args); va_end(args);
}

}  // namespace fms::log
