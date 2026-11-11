// GPL-3.0-or-later. Board-owned, finite DWIN operations for the verified MKS profile.
#ifdef JD_DGUS_NATIVE
#include "JDDgusNative.h"
#include "Machine/MachineConfig.h"
#include "Machine/Axes.h"
#include "Machine/Axis.h"
#include "CoolantControl.h"
#include "Driver/Console.h"
#include "GCode.h"
#include "Job.h"
#include "Planner.h"
#include "Protocol.h"
#include "Settings.h"
#include "System.h"
#include <Arduino.h>
#include <atomic>
#include <cmath>
#include <cstring>
#include <cstdio>
#include <limits>
#include <algorithm>

namespace JDDgusNative {
using Machine::Axes;
namespace {
constexpr uint16_t touchVp = 0x1000, stepVp = 0x1200, speedVp = 0x1201;
constexpr uint8_t ok = 0, blocked = 1, busy = 3, bounds = 4, unknown = 5, invalid = 6, failed = 7, waitPc = 10;
constexpr uint8_t pcOffline = 13;
constexpr int32_t steps[] = { 100, 1000, 5000, 10000, 50000, 100000 };
constexpr uint32_t leaseMs = 3000, liftMs = 250;
enum class JogPhase : uint8_t { None, Raising, Moving };

QueueHandle_t requests = nullptr, events = nullptr, states = nullptr;
std::atomic<bool> enabled{false};
std::atomic<bool> stopRequested{false};
std::atomic<bool> resetRequested{false};
std::atomic<uint32_t> liveSession{0}, lastHeartbeat{0}, livePeer{0};
// Only odd generations are online. A quick reconnection still invalidates
// earlier touches and any move that was started under the previous link.
std::atomic<uint32_t> panelGeneration{0}, jogPanelGeneration{0};
State state;
uint32_t nextRequestId = 0, lastPublished = 0;
JogPhase jogPhase = JogPhase::None;
uint32_t jogSince = 0, jogBudget = 0;
int32_t dx = 0, dy = 0;
float jogFeed = 0;
uint8_t jogKey = 0;

bool idle() {
    return state_is(::State::Idle) && !sys.abort() && !Job::active() && !plan_get_current_block();
}
bool stopped() {
    return (state_is(::State::Idle) || state_is(::State::Alarm)) && !sys.abort() &&
           !Job::active() && !plan_get_current_block();
}

void readPosition() {
    steps_t motor[MAX_N_AXIS] = {};
    float position[MAX_N_AXIS] = {};
    get_steps(motor);
    steps_to_mpos(position, motor);
    state.x = int32_t(std::lround(double(position[X_AXIS]) * 1000));
    state.y = int32_t(std::lround(double(position[Y_AXIS]) * 1000));
}

void publish() {
    readPosition();
    state.ready = idle() && jogPhase == JogPhase::None && state.owner == 0;
    state.busy = !stopped() || jogPhase != JogPhase::None;
    xQueueOverwrite(states, &state);
    lastPublished = millis();
}

void note(uint16_t vp, uint8_t key, uint8_t result, Decision decision = Decision::None, uint32_t session = 0) {
    state.result = result;
    // Publish before returning any decision about CNC ownership.
    publish();
    Event event{session, decision, vp, key, result};
    xQueueSend(events, &event, 0);
}

void clearPending(State& target) {
    target.pending = target.pendingKey = 0;
    target.pendingSession = target.pendingPanelGeneration = 0;
}

bool pendingLinkCurrent(const State& target) {
    return target.pendingSession == liveSession.load() && target.pendingPanelGeneration == panelGeneration.load();
}

void clearCalibration() {
    state.calibrated = state.sheet = false;
    state.originX = state.originY = 0;
    state.width = state.height = 0;
    clearPending(state);
    ++state.generation;
}

void cancelState() {
    clearCalibration();
    state.owner = 0;
    jogPhase = JogPhase::None;
    jogPanelGeneration.store(0);
    // Realtime reset is executed in FluidNC and stops coolant and stepping.
}

bool configMatches() {
    if (!config || config->_board != "MKS DLC32 v2.2" || config->_name != "JD_Plotter_DLC32_V22" ||
        config->_reportInches || !config->_coolant ||
        std::strcmp(config->_coolant->floodPinName(), "gpio.32:low") ||
        config->_coolant->hasMist() || Axes::_numberAxis < 2 ||
        !config->_start || config->_start->_mustHome) return false;
    const auto x = Axes::_axis[X_AXIS], y = Axes::_axis[Y_AXIS];
    if (!x || !y || !x->_motors[0] || !y->_motors[0] || !y->_motors[1]) return false;
    return std::isfinite(x->_maxRate) && x->_maxRate > 0 &&
           std::isfinite(y->_maxRate) && y->_maxRate > 0 &&
           std::isfinite(x->_maxTravel) && x->_maxTravel > 0 &&
           std::isfinite(y->_maxTravel) && y->_maxTravel > 0;
}

bool isG54() {
    if (gc_state.modal.coord_select != CoordIndex::G54) return false;
    const int32_t offsets[] = { state.originX, state.originY };
    for (unsigned axis = 0; axis < 2; ++axis) {
        if (!std::isfinite(gc_state.coord_system[axis]) ||
            std::abs(double(gc_state.coord_system[axis]) * 1000 - offsets[axis]) > 1.1 ||
            gc_state.coord_offset[axis] != 0 || gc_state.tool_length_offset[axis] != 0) return false;
    }
    return true;
}

bool execute(const char* command) {
    const Error result = execute_line(command, Console, AuthenticationLevel::LEVEL_GUEST, true);
    return result == Error::Ok && !sys.abort() && !stopRequested.load();
}

bool contains(int64_t x, int64_t y) {
    if (!state.sheet) return true;
    const auto paperX = (x - state.originX) * paperDirection;
    const auto paperY = (y - state.originY) * paperDirection;
    return paperX >= 0 && paperX <= state.width && paperY >= 0 && paperY <= state.height;
}

uint8_t calibrate(uint8_t which) {
    if (which == 0) {
        clearCalibration();
        return ok;
    }
    readPosition();
    if (which == 1) {
        clearCalibration();
        if (!execute("G21 G90 G94 G54 G92.1 G49") || !execute("G10 L20 P1 X0 Y0")) return failed;
        state.originX = state.x;
        state.originY = state.y;
        state.calibrated = isG54();
        return state.calibrated ? ok : failed;
    }
    if (which != 2) return invalid;
    if (!state.calibrated) return bounds;
    const int64_t width = (int64_t(state.x) - state.originX) * paperDirection;
    const int64_t height = (int64_t(state.y) - state.originY) * paperDirection;
    if (width < 1000 || height < 1000 ||
        width > double(Axes::_axis[X_AXIS]->_maxTravel) * 1000 ||
        height > double(Axes::_axis[Y_AXIS]->_maxTravel) * 1000) return bounds;
    state.width = uint32_t(width);
    state.height = uint32_t(height);
    state.sheet = true;
    state.pending = state.pendingKey = 0;
    ++state.generation;
    return ok;
}

uint8_t beginJog(uint8_t key, uint32_t now, uint32_t generation) {
    readPosition();
    const int32_t distance = steps[state.step];
    dx = key == 1 ? -distance : key == 2 ? distance : 0;
    dy = key == 3 ? -distance : key == 4 ? distance : 0;
    if (!contains(int64_t(state.x) + dx, int64_t(state.y) + dy)) return bounds;
    const auto axis = Axes::_axis[dx ? X_AXIS : Y_AXIS];
    if (distance > double(axis->_maxTravel) * 1000) return bounds;
    jogFeed = std::min(axis->_maxRate, 300.0f) * state.speed / 100.0f;
    const double travelMs = 60.0 * distance / jogFeed;
    if (!std::isfinite(travelMs) || travelMs > 2'100'000) return invalid;
    jogBudget = uint32_t(travelMs) + 15000;
    jogKey = key;
    jogSince = now;
    jogPhase = JogPhase::Raising;
    jogPanelGeneration.store(generation);
    if (execute("M8") && config->_coolant->get_state().Flood) return ok;
    jogPhase = JogPhase::None;
    jogPanelGeneration.store(0);
    config->_coolant->stop();
    return failed;
}

void endJog(uint8_t result, bool reset) {
    const uint8_t key = jogKey;
    jogPhase = JogPhase::None;
    jogPanelGeneration.store(0);
    if (reset) {
        config->_coolant->stop();
        clearCalibration();
        protocol_send_event(&rtResetEvent);
    } else if (!execute("M9")) {
        config->_coolant->stop();
        clearCalibration();
        protocol_send_event(&rtResetEvent);
        result = failed;
    }
    note(touchVp, key, result);
}

void advanceJog(uint32_t now) {
    if (jogPhase == JogPhase::None) return;
    if (sys.abort() || state_is(::State::Alarm) || state_is(::State::ConfigAlarm) ||
        state_is(::State::SafetyDoor) || state_is(::State::Hold) || Job::active() ||
        uint32_t(now - jogSince) > jogBudget) {
        endJog(failed, true);
        return;
    }
    if (jogPhase == JogPhase::Raising && uint32_t(now - jogSince) >= liftMs) {
        if (!idle() || !config->_coolant->get_state().Flood) {
            endJog(failed, true);
            return;
        }
        static char jog[90];
        std::snprintf(jog, sizeof(jog), "$J=G91 G21 X%.3f Y%.3f F%.3f", dx / 1000.0, dy / 1000.0, jogFeed);
        if (!execute(jog)) {
            endJog(failed, true);
            return;
        }
        jogPhase = JogPhase::Moving;
    } else if (jogPhase == JogPhase::Moving && idle()) {
        endJog(ok, false);
    }
}

void perform(Request request, uint32_t now) {
    if (request.session && request.session != liveSession.load()) return;
    if (!request.session && (!(request.panelGeneration & 1) ||
                             request.panelGeneration != panelGeneration.load())) return;
    if (request.action == Action::Claim) {
        const bool acquired = request.session && !state.owner && stopped() && jogPhase == JogPhase::None &&
                              uint32_t(now - lastHeartbeat.load()) <= leaseMs;
        if (acquired) state.owner = request.session;
        note(0, 0, acquired ? ok : blocked, Decision::Claim, request.session);
        return;
    }
    if (request.action == Action::Release) {
        const bool released = state.owner == request.session && stopped() && !config->_coolant->get_state().Flood;
        if (released) state.owner = 0;
        note(0, 0, released ? ok : blocked, Decision::Release, request.session);
        return;
    }
    if (request.action == Action::Result) {
        if (request.id && state.pending == request.id && request.session == state.pendingSession) {
            const auto key = state.pendingKey;
            clearPending(state);
            note(touchVp, key, uint8_t(request.value));
        }
        return;
    }
    const uint16_t vp = request.action == Action::Step ? stepVp : request.action == Action::Speed ? speedVp : touchVp;
    const uint8_t key = uint8_t(request.value);
    if (request.action == Action::Button && key == 31) {
        // A distinct operator action, never unlock as a side effect of jogging.
        const bool unlocked = !state.owner && jogPhase == JogPhase::None && stopped() &&
                              (idle() || (state_is(::State::Alarm) && execute("$X") && idle()));
        note(vp, key, unlocked ? ok : blocked);
        return;
    }
    if (state.owner || jogPhase != JogPhase::None || !idle()) {
        note(vp, key, busy);
        return;
    }
    if (request.action == Action::Step) {
        if (request.value < sizeof(steps) / sizeof(steps[0])) {
            state.step = key;
            note(vp, key, ok);
        } else note(vp, key, invalid);
    } else if (request.action == Action::Speed) {
        if (request.value >= 1 && request.value <= 100) {
            state.speed = key;
            note(vp, key, ok);
        } else note(vp, key, invalid);
    } else if (request.action == Action::Calibrate) {
        note(vp, key, calibrate(key));
    } else if (key >= 1 && key <= 4) {
        const uint8_t result = beginJog(key, now, request.panelGeneration);
        if (result != ok) note(vp, key, result);
    } else if (key == 10 || key == 11) {
        note(vp, key, calibrate(uint8_t(key - 9)));
    } else if (key == 12 || key == 13 || (key >= 15 && key <= 18)) {
        if ((key == 12 || key == 13 || key == 15 || key == 16) && !state.sheet) {
            note(vp, key, bounds);
        } else if (!request.panelPcSession || request.panelPcSession != liveSession.load()) {
            note(vp, key, pcOffline);
        } else {
            if (++nextRequestId == 0) ++nextRequestId;
            state.pending = nextRequestId;
            state.pendingKey = key;
            state.pendingSession = request.panelPcSession;
            state.pendingPanelGeneration = request.panelGeneration;
            note(vp, key, waitPc);
        }
    } else note(vp, key, unknown);
}
}  // namespace

bool init() {
    if (!configMatches()) return false;
    requests = xQueueCreate(8, sizeof(Request));
    events = xQueueCreate(16, sizeof(Event));
    states = xQueueCreate(1, sizeof(State));
    if (!requests || !events || !states) {
        if (requests) vQueueDelete(requests);
        if (events) vQueueDelete(events);
        if (states) vQueueDelete(states);
        requests = events = states = nullptr;
        return false;
    }
    enabled.store(true);
    return true;
}

bool submit(Request request) {
    if (!request.session) {
        request.panelGeneration = panelGeneration.load();
        request.panelPcSession = liveSession.load();
    }
    return requests && xQueueSend(requests, &request, 0) == pdTRUE;
}
bool receive(Event& event) { return events && xQueueReceive(events, &event, 0) == pdTRUE; }
bool snapshot(State& out) {
    if (!states || xQueuePeek(states, &out, 0) != pdTRUE) return false;
    if (out.pending && !pendingLinkCurrent(out)) {
        clearPending(out);
        out.result = pcOffline;
    }
    return true;
}

void setPanelOnline(bool online) {
    const auto current = panelGeneration.load();
    if (bool(current & 1) != online) panelGeneration.store(current + 1);
}

void connect(uint32_t session, uint32_t peer) {
    lastHeartbeat.store(millis());
    livePeer.store(peer);
    liveSession.store(session);
}
void heartbeat(uint32_t session, uint32_t now) {
    if (session == liveSession.load()) lastHeartbeat.store(now);
}
void disconnect(uint32_t session) {
    if (session == liveSession.load()) liveSession.store(0);
}
void stop() { resetRequested.store(true); }
bool takeRealtimeStop() {
    if (!enabled.load()) return false;
    const bool requested = resetRequested.exchange(false);
    const bool lost = state.owner && (state.owner != liveSession.load() ||
                                      uint32_t(millis() - lastHeartbeat.load()) > leaseMs);
    const auto jogGeneration = jogPanelGeneration.load();
    const bool lostPanel = jogGeneration && jogGeneration != panelGeneration.load();
    if (!requested && !lost && !lostPanel) return false;
    // Clear owner before nested realtime checks can process another reset.
    state.owner = 0;
    jogPanelGeneration.store(0);
    stopRequested.store(true);
    return true;
}
void onReset() {
    // Called before the FluidNC reset clears the planner. Invalidate all UI
    // state when the protocol task next returns to its top-level loop.
    if (enabled.load()) {
        state.owner = 0;
        stopRequested.store(true);
    }
}

void service() {
    if (!enabled.load()) return;
    const uint32_t now = millis();
    if (stopRequested.exchange(false)) {
        xQueueReset(requests);
        cancelState();
        note(touchVp, 14, ok);
        return;
    }
    if (takeRealtimeStop()) {
        rtResetEvent.run(nullptr);
        return;
    }
    if (state.pending && !pendingLinkCurrent(state)) {
        const auto key = state.pendingKey;
        clearPending(state);
        note(touchVp, key, pcOffline);
    }
    if (state.calibrated && !isG54()) {
        clearCalibration();
        publish();
    }
    advanceJog(now);
    Request request{};
    if (xQueueReceive(requests, &request, 0) == pdTRUE) perform(request, now);
    if (uint32_t(now - lastPublished) >= 100) publish();
}

bool allowsExternalLine(const char* line, uint32_t peer) {
    if (!enabled.load()) return true;
    for (auto readOnly : {"", "$I", "$$", "$#", "$G", "$Config/Dump", "$SS", "$BS", "$Heap"}) {
        if (!std::strcmp(line, readOnly)) return true;
    }
    return peer && peer == livePeer.load() && state.owner && state.owner == liveSession.load() &&
           uint32_t(millis() - lastHeartbeat.load()) <= leaseMs && !stopRequested.load() &&
           !resetRequested.load();
}
}
#endif
