// Jerusalem Dynamics DWIN control. IO and CNC tasks exchange bounded messages.
#pragma once
#include <cstdint>

namespace JDDgusNative {
// This installation moves right/down on negative X/Y with the current wiring.
inline constexpr int32_t paperDirection = -1;
enum class Action : uint8_t { Button, Step, Speed, Calibrate, Claim, Release, Result };
enum class Decision : uint8_t { None, Claim, Release };

struct Request {
    Action action;
    uint16_t value;
    uint32_t session = 0;
    uint32_t id = 0;
    uint32_t panelGeneration = 0;
    uint32_t panelPcSession = 0;
};

struct State {
    uint32_t generation = 0, owner = 0, pending = 0;
    uint32_t pendingSession = 0, pendingPanelGeneration = 0;
    int32_t x = 0, y = 0, originX = 0, originY = 0;
    uint32_t width = 0, height = 0;
    uint8_t pendingKey = 0, result = 0, step = 1, speed = 50;
    bool calibrated = false, sheet = false, ready = false, busy = false;
};

struct Event {
    uint32_t session;
    Decision decision;
    uint16_t vp;
    uint8_t key, result;
};

bool init();
bool submit(Request request);
bool receive(Event& event);
bool snapshot(State& state);
void setPanelOnline(bool online);
void connect(uint32_t session, uint32_t peer);
void heartbeat(uint32_t session, uint32_t now);
void disconnect(uint32_t session);
void stop();
bool takeRealtimeStop();
void onReset();
void service();
bool allowsExternalLine(const char* line, uint32_t peer);
}
