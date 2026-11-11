// GPL-3.0-or-later. DWIN serial traffic stays on the board.
#ifdef JD_DGUS_NATIVE
#include "JDDgusNative.h"
#include "Machine/MachineConfig.h"
#include "Module.h"
#include "Report.h"
#include "Uart.h"
#include "Driver/fluidnc_uart.h"
#include <driver/uart.h>
#include "fnc_idf_uart.h"
#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>
#include <lwip/sockets.h>
#include <lwip/tcp.h>
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>

namespace {
using namespace JDDgusNative;
using PanelState = JDDgusNative::State;
using PanelEvent = JDDgusNative::Event;
constexpr uint16_t currentPageVp = 0x0014, touchVp = 0x1000, statusVp = 0x1100, resultVp = 0x1110, stepVp = 0x1200;
constexpr uint8_t lease = 0x10, ack = 0x11, release = 0x13, stateFrame = 0x20, eventFrame = 0x21;
constexpr uint8_t pcStatus = 0x22, pcResult = 0x23, calibration = 0x24, hello = 0x7f;
constexpr uint8_t probe = 0x25, pong = 0x26;
constexpr uint8_t panelQuery = 0x27, panelAnswer = 0x28, panelSheet = 0x29, panelReady = 0x2a;
constexpr uint8_t ask = 1, micStart = 2, micStop = 3, cancel = 4, viewSheet = 5;
constexpr uint8_t composing = 3, answerPage = 5, sheetPage = 6, latin = 8, home = 1;
constexpr uint16_t keyboardPrefix = 0x4000;
constexpr uint16_t keyboardVp = 0x1001, assistantVp = 0x1300, questionVp = 0x1400, answerVp = 0x1480, graphVp = 0x2000;
constexpr size_t maxQuestion = 120, maxAnswer = 112, maxSegments = 160, textCells = 56;
constexpr uint16_t viewX = 190, viewY = 90, viewWidth = 420, viewHeight = 278;
constexpr uint32_t jobTimeout = 125000, previewTimeout = 8000;
constexpr size_t logSize = 32;

template <size_t N> struct Output {
    uint8_t bytes[N]{};
    size_t start = 0, end = 0;
    size_t size() const { return end - start; }
    size_t space() const { return N - size(); }
    void clear() { start = end = 0; }
    bool append(const uint8_t* data, size_t count) {
        if (count > space()) return false;
        if (end + count > N) {
            const size_t remaining = size();
            std::memmove(bytes, bytes + start, remaining);
            start = 0;
            end = remaining;
        }
        std::memcpy(bytes + end, data, count);
        end += count;
        return true;
    }
    void consume(size_t count) { start += count; if (start == end) clear(); }
};

void put32(uint8_t*& out, uint32_t value) {
    for (int shift = 24; shift >= 0; shift -= 8) *out++ = uint8_t(value >> shift);
}
uint32_t get32(const uint8_t* data) {
    return uint32_t(data[0]) << 24 | uint32_t(data[1]) << 16 | uint32_t(data[2]) << 8 | data[3];
}
uint16_t word(const uint8_t* data) { return uint16_t(data[0]) << 8 | data[1]; }
bool wouldWait() { return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR; }

class Panel : public Module {
    Uart* uart = nullptr;
    bool enabled = false, drain = true, panelOnline = false, loaded = false;
    uint16_t latchedKey = 0;
    uint8_t progress = 0;
    enum class Poll { None, Clear, Page, OnlinePage, Selectors, Touch, Keyboard, Question };
    Poll waiting = Poll::None;
    uint32_t epoch = 0, pollAt = 0, pollSince = 0, panelReplyAt = 0, serialByteAt = 0, lastDisplay = 0, lastState = 0;
    uint32_t pageCheckAt = 0;
    bool pageReadable = false, splashVisible = false;
    uint16_t seenStep = 1, seenSpeed = 50;
    Output<256> serial;
    uint8_t serialIn[11]{};
    size_t serialCount = 0;
    int listener = -1, peer = -1;
    uint32_t bootId = 0, session = 0, logSeq = 0, logNext = 1, listenerAttemptAt = 0;
    Output<512> network;
    uint8_t incoming[6 + 4 + 8 + 8 * maxSegments]{};
    size_t inputCount = 0, inputWanted = 6;
    struct LogEntry { uint32_t sequence, at; PanelEvent event; };
    LogEntry logs[logSize]{};
    size_t logCount = 0;
    uint32_t probeId = 0, probeSince = 0;
    uint8_t probeResult = 0;
    bool interactivePc = false;
    uint32_t splashSince = 0;
    uint8_t page = home, queuedPage = 0, stage = 0, previewStatus = 0;
    uint32_t nextUiId = 0, assistantId = 0, previewId = 0, assistantAt = 0, previewAt = 0;
    uint8_t question[maxQuestion]{}, answer[maxAnswer]{};
    size_t questionSize = 0, answerSize = 0, answerOffset = 0;
    uint16_t shownQuestion[textCells]{}, shownAnswer[textCells]{};
    bool questionWritten = false, answerWritten = false, auditQuestion = false;
    bool stageDirty = false, questionDirty = false, answerDirty = false, graphClear = false;
    uint16_t graph[3 + 5 * (4 + maxSegments)]{};
    size_t graphSize = 0, graphOffset = 0;

    void logEvent(PanelEvent event, uint32_t now) {
        ++logSeq;
        logs[(logSeq - 1) % logSize] = {logSeq, now, event};
        logCount = std::min(logCount + 1, logSize);
    }
    void finishProbe(uint8_t result, uint32_t now) {
        probeResult = result;
        probeSince = now;
        logEvent({0, Decision::None, touchVp, 22, result}, now);
    }
    uint8_t currentResult(const PanelState& s) const {
        return s.owner ? 3 : s.pending ? 10 : probeResult ? probeResult : s.result;
    }
    void testPc(uint32_t now) {
        if (probeResult == 11) return;
        if (++probeId == 0) ++probeId;
        uint8_t payload[4];
        auto* out = payload;
        put32(out, probeId);
        if (peer >= 0 && sendFrame(probe, payload, sizeof(payload))) finishProbe(11, now);
        else finishProbe(13, now);
    }

    bool sendFrame(uint8_t type, const uint8_t* data, size_t length) {
        const uint8_t header[] = {'J','D',1,type,uint8_t(length >> 8),uint8_t(length)};
        return length + sizeof(header) <= network.space() &&
               network.append(header, sizeof(header)) && network.append(data, length);
    }
    bool sendState(const PanelState& s) {
        uint8_t data[41]{};
        auto* out = data;
        *out++ = 1 | (s.calibrated ? 2 : 0) | (s.sheet ? 4 : 0) | (s.owner == session && session ? 8 : 0) |
                 (s.busy ? 16 : 0) | (panelOnline ? 32 : 0) | (loaded ? 64 : 0) |
                 (paperDirection < 0 ? 128 : 0);
        *out++ = s.step;
        *out++ = s.speed;
        for (auto v : {s.x, s.y, s.originX, s.originY}) put32(out, uint32_t(v));
        const bool pendingForPeer = s.pending && s.pendingSession == session && panelOnline;
        for (auto v : {s.width, s.height, s.generation, bootId, pendingForPeer ? s.pending : 0}) put32(out, v);
        *out++ = pendingForPeer ? s.pendingKey : 0;
        *out++ = currentResult(s);
        return sendFrame(stateFrame, data, size_t(out - data));
    }
    bool sendAck(const PanelState& s) {
        const uint8_t data[] = {uint8_t(s.owner == session && session), 0};
        return sendFrame(ack, data, sizeof(data));
    }
    void closePeer() {
        if (peer >= 0) {
            JDDgusNative::disconnect(session);
            shutdown(peer, SHUT_RDWR);
            close(peer);
        }
        peer = -1;
        network.clear();
        inputCount = 0;
        inputWanted = 6;
        loaded = false;
        progress = 0;
        interactivePc = false;
        if (assistantId) finishAssistant(6);
        if (previewId) finishPreview(3);
        if (probeResult == 11) finishProbe(13, millis());
    }
    void startListener(uint32_t now) {
        listenerAttemptAt = now;
        listener = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (listener < 0) return;
        const int yes = 1;
        setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(8888);
        address.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 ||
            listen(listener, 1) < 0 || fcntl(listener, F_SETFL, O_NONBLOCK) < 0) {
            close(listener);
            listener = -1;
        }
    }
    void acceptPeer() {
        if (listener < 0 || peer >= 0) return;
        sockaddr_in address{};
        socklen_t size = sizeof(address);
        peer = accept(listener, reinterpret_cast<sockaddr*>(&address), &size);
        if (peer < 0) return;
        if (fcntl(peer, F_SETFL, O_NONBLOCK) < 0) { closePeer(); return; }
        const int yes = 1, keepIdle = 10, keepInterval = 3, keepCount = 3;
        setsockopt(peer, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
        setsockopt(peer, SOL_SOCKET, SO_KEEPALIVE, &yes, sizeof(yes));
        setsockopt(peer, IPPROTO_TCP, TCP_KEEPIDLE, &keepIdle, sizeof(keepIdle));
        setsockopt(peer, IPPROTO_TCP, TCP_KEEPINTVL, &keepInterval, sizeof(keepInterval));
        setsockopt(peer, IPPROTO_TCP, TCP_KEEPCNT, &keepCount, sizeof(keepCount));
        if (++session == 0) ++session;
        JDDgusNative::connect(session, address.sin_addr.s_addr);
        logNext = logSeq - uint32_t(logCount) + 1;
        lastState = 0;
        constexpr uint8_t banner[] = {'J','D','-','D','G','U','S','/','2'};
        sendFrame(hello, banner, sizeof(banner));
    }
    bool handlePacket(uint32_t now, const PanelState& s) {
        const auto* data = incoming + 6;
        const size_t len = inputWanted - 6;
        switch (incoming[3]) {
            case lease:
                if (len != 1 || data[0] > 2) return false;
                if (data[0] == 0) {
                    if (s.owner == session) {
                        JDDgusNative::stop();
                        const uint8_t stopped[] = {0, 1};
                        return sendFrame(ack, stopped, sizeof(stopped));
                    }
                    return sendAck(s);
                }
                if (data[0] == 2) {
                    if (s.owner == session) JDDgusNative::heartbeat(session, now);
                    return sendAck(s);
                }
                JDDgusNative::heartbeat(session, now);
                return submit({Action::Claim, 0, session});
            case release:
                return len == 0 && submit({Action::Release, 0, session});
            case pcStatus:
                if (len != 2 || data[0] > 1 || data[1] > 100) return false;
                loaded = data[0];
                progress = data[1];
                return true;
            case pcResult:
                if (len != 5 || !get32(data) || (data[4] != 0 && data[4] != 4 && data[4] != 7 && data[4] != 9)) return false;
                return submit({Action::Result, data[4], session, get32(data)});
            case calibration:
                return len == 1 && data[0] <= 2 && submit({Action::Calibrate, data[0], session});
            case pong:
                if (len != 4) return false;
                if (probeResult == 11 && get32(data) == probeId && uint32_t(now - probeSince) < 1500)
                    finishProbe(12, now);
                return true;
            case panelReady:
                if (len != 0) return false;
                interactivePc = true;
                return true;
            case panelAnswer:
                if (!interactivePc || len < 5 || len > 5 + maxAnswer || data[4] < 1 || data[4] > 6) return false;
                if (assistantId && get32(data) == assistantId) {
                    finishAssistant(data[4], data + 5, len - 5);
                    logEvent({0, Decision::None, answerVp, uint8_t(len - 5), data[4]}, now);
                    assistantAt = now;
                }
                return true;
            case panelSheet:
                if (!interactivePc || len < 4 || (len != 4 && (len < 12 || (len - 4) % 8))) return false;
                if (previewId && get32(data) == previewId) {
                    if (len == 4) finishPreview(0);
                    else if (!prepareGraph(data + 4, (len - 12) / 8)) finishPreview(3);
                    else previewId = 0;
                }
                return true;
            default: return false;
        }
    }
    void serviceNetwork(uint32_t now, PanelState& s, bool haveState) {
        if (listener < 0 && uint32_t(now - listenerAttemptAt) >= 1000) startListener(now);
        acceptPeer();
        if (peer >= 0) {
            uint8_t bytes[96];
            const int count = recv(peer, bytes, sizeof(bytes), MSG_DONTWAIT);
            if (count == 0 || (count < 0 && !wouldWait())) closePeer();
            for (int i = 0; i < count && peer >= 0; ++i) {
                incoming[inputCount++] = bytes[i];
                if (inputCount == 6) {
                    const size_t length = word(incoming + 4);
                    if (incoming[0] != 'J' || incoming[1] != 'D' || incoming[2] != 1 ||
                        length > (incoming[3] == panelSheet ? sizeof(incoming) - 6 : 5 + maxAnswer)) {
                        closePeer(); break;
                    }
                    inputWanted = 6 + length;
                }
                if (inputCount == inputWanted) {
                    if (!handlePacket(now, s)) { closePeer(); break; }
                    inputCount = 0;
                    inputWanted = 6;
                }
            }
        }
        PanelEvent event{};
        for (unsigned i = 0; i < 16 && JDDgusNative::receive(event); ++i) {
            if (event.vp) logEvent(event, now);
            if (peer >= 0 && event.decision != Decision::None && event.session == session) {
                if (!snapshot(s) || !sendState(s) || !sendAck(s)) closePeer();
                lastState = now;
            }
        }
        if (peer < 0) return;
        if (haveState && uint32_t(now - lastState) >= 150 && sendState(s)) lastState = now;
        const auto oldest = logSeq - uint32_t(logCount) + 1;
        if (uint32_t(logSeq - logNext + 1) > logCount) logNext = oldest;
        for (unsigned i = 0; i < 4 && logNext != logSeq + 1; ++i) {
            if (network.space() < 96) break;
            const auto& entry = logs[(logNext - 1) % logSize];
            uint8_t data[12];
            auto* out = data;
            put32(out, entry.sequence);
            put32(out, entry.at);
            *out++ = entry.event.key;
            *out++ = entry.event.result;
            *out++ = uint8_t(entry.event.vp >> 8);
            *out++ = uint8_t(entry.event.vp);
            sendFrame(eventFrame, data, sizeof(data));
            ++logNext;
        }
        if (network.size()) {
            const int sent = send(peer, network.bytes + network.start, network.size(), MSG_DONTWAIT);
            if (sent > 0) network.consume(size_t(sent));
            else if (sent == 0 || !wouldWait()) closePeer();
        }
    }

    void readVp(uint16_t vp, uint8_t count) {
        const uint8_t data[] = {0x5a, 0xa5, 4, 0x83, uint8_t(vp >> 8), uint8_t(vp), count};
        serial.append(data, sizeof(data));
    }
    void writeVp(uint16_t vp, const uint16_t* words, size_t count) {
        uint8_t data[246] = {0x5a, 0xa5, uint8_t(3 + 2 * count), 0x82, uint8_t(vp >> 8), uint8_t(vp)};
        for (size_t i = 0; i < count; ++i) {
            data[6 + 2 * i] = uint8_t(words[i] >> 8);
            data[7 + 2 * i] = uint8_t(words[i]);
        }
        serial.append(data, 6 + 2 * count);
    }
    void writeText(uint16_t vp, const uint8_t* data, size_t size,
                   uint16_t (&shown)[textCells], bool& initialized) {
        uint16_t words[textCells]{};
        for (size_t i = 0; i < std::min(size, textCells); ++i) words[i] = data[i];
        size_t first = 0, end = textCells;
        if (initialized) {
            while (first < end && words[first] == shown[first]) ++first;
            while (first < end && words[end - 1] == shown[end - 1]) --end;
        }
        if (first < end) writeVp(uint16_t(vp + first), words + first, end - first);
        std::memcpy(shown, words, sizeof(words));
        initialized = true;
    }
    uint32_t uiId() {
        if (++nextUiId == 0) ++nextUiId;
        return nextUiId;
    }
    bool requestPc(uint32_t id, uint8_t action, const uint8_t* data = nullptr, size_t size = 0) {
        if (!interactivePc || peer < 0) return false;
        uint8_t message[5 + maxQuestion];
        auto* out = message;
        put32(out, id);
        *out++ = action;
        if (size) std::memcpy(out, data, size);
        return sendFrame(panelQuery, message, 5 + size);
    }
    void showPage(uint8_t id) {
        page = queuedPage = id;
    }
    void finishAssistant(uint8_t value, const uint8_t* text = nullptr, size_t size = 0) {
        stage = value;
        stageDirty = true;
        answerSize = size;
        answerOffset = 0;
        if (size) std::memcpy(answer, text, size);
        answerDirty = true;
        if (value >= 5) assistantId = 0;
    }
    void finishPreview(uint8_t value) {
        previewStatus = value;
        stageDirty = true;
        previewId = 0;
    }
    bool prepareGraph(const uint8_t* data, size_t segments) {
        if (segments > maxSegments) return false;
        auto valid = [](const uint8_t* p) {
            return word(p) < viewWidth && word(p + 4) < viewWidth &&
                   word(p + 2) < viewHeight && word(p + 6) < viewHeight;
        };
        if (!valid(data) || word(data) >= word(data + 4) || word(data + 2) >= word(data + 6)) return false;
        for (size_t i = 1; i <= segments; ++i) if (!valid(data + i * 8)) return false;
        const uint16_t left = word(data), top = word(data + 2), right = word(data + 4), bottom = word(data + 6);
        graph[0] = 0x000a;
        graph[1] = uint16_t(segments + 4);
        size_t offset = 2;
        auto line = [&](uint16_t color, uint16_t x, uint16_t y, uint16_t x2, uint16_t y2) {
            for (uint16_t value : {color, uint16_t(x + viewX), uint16_t(y + viewY),
                                   uint16_t(x2 + viewX), uint16_t(y2 + viewY)}) graph[offset++] = value;
        };
        line(0x7bef, left, top, right, top);
        line(0x7bef, right, top, right, bottom);
        line(0x7bef, right, bottom, left, bottom);
        line(0x7bef, left, bottom, left, top);
        for (size_t i = 0; i < segments; ++i) {
            data += 8;
            line(0xffff, word(data), word(data + 2), word(data + 4), word(data + 6));
        }
        graph[offset++] = 0xff00;
        graphSize = offset;
        graphOffset = 1;  // Hold the first word disabled until the entire graph is written.
        return true;
    }
    bool keyboardPage() const { return page == composing || page == latin; }
    void typeKey(uint16_t key, uint32_t now) {
        const size_t before = questionSize;
        if (key == 0x1b) {
            questionSize = 0;
        } else if (key == 8) {
            if (questionSize) --questionSize;
        } else if (questionSize < maxQuestion) {
            uint8_t value = 0;
            if (key >= 0x20 && key <= 0x7e) value = key;
            else if (key >= 0x0410 && key <= 0x044f) value = uint8_t(key - 0x0410 + 0xc0);
            else if (key == 0x0401 || key == 0x0451) value = key == 0x0401 ? 0xa8 : 0xb8;
            if (value) question[questionSize++] = value;
        }
        if (questionSize != before) {
            questionDirty = true;
            // Record the input length only. Never log letters or answers.
            logEvent({0, Decision::None, keyboardVp, uint8_t(questionSize), 0}, now);
        }
    }
    bool interactiveKey(uint16_t key, uint32_t now) {
        if (key == 20 || key == 30 || key == 27 || key == 28) {
            if (assistantId) return true;
            if (key == 20 || key == 30) {
                questionSize = 0;
                finishAssistant(0);
            }
            showPage(key == 27 ? latin : composing);
            questionDirty = true;
        } else if (key == 21 || key == 26) {
            if (key == 26 && assistantId && stage == 3) {
                if (requestPc(assistantId, micStop)) finishAssistant(4);
                else finishAssistant(6);
                return true;
            }
            if (!keyboardPage() || assistantId) return true;
            size_t textSize = questionSize;
            while (textSize && question[textSize - 1] == ' ') --textSize;
            if (key == 21 && !textSize) return true;
            assistantId = uiId();
            assistantAt = now;
            finishAssistant(1);
            showPage(answerPage);
            if (key == 21 ? !requestPc(assistantId, ask, question, textSize) : !requestPc(assistantId, micStart))
                finishAssistant(6);
        } else if (key == 29) {
            if (page == answerPage && answerSize > textCells) {
                answerOffset = answerOffset ? 0 : textCells;
                answerDirty = true;
            }
        } else if (key == 23) {
            if (previewId || graphSize) return true;
            // The active screen is the only place that can stop the recording
            // or cancel its request. Keep it visible until it is finished.
            if (assistantId) return true;
            showPage(sheetPage);
            graphClear = true;
            graphSize = 0;
            previewId = uiId();
            previewAt = now;
            previewStatus = 1;
            stageDirty = true;
            if (!requestPc(previewId, viewSheet)) finishPreview(3);
        } else if (key == 24 || key == 25) {
            if (key == 24) {
                if (assistantId) requestPc(assistantId, cancel);
                assistantId = 0;
                finishAssistant(0);
            } else {
                graphSize = 0;
                finishPreview(0);
            }
            showPage(home);
        } else return false;
        logEvent({0, Decision::None, touchVp, uint8_t(key), 0}, now);
        return true;
    }
    void serviceInteractiveDisplay() {
        if (serial.size()) return;
        if (queuedPage) {
            const uint16_t values[] = {0x5a01, queuedPage};
            writeVp(0x0084, values, 2);
            queuedPage = 0;
            pageCheckAt = millis();
        } else if (graphClear) {
            const uint16_t clear = 0xff00;
            writeVp(graphVp, &clear, 1);
            graphClear = false;
        } else if (stageDirty) {
            const uint16_t values[] = {stage, previewStatus};
            writeVp(assistantVp, values, 2);
            stageDirty = false;
        } else if (questionDirty) {
            const auto offset = questionSize > textCells ? questionSize - textCells : 0;
            writeText(questionVp, question + offset, questionSize - offset, shownQuestion, questionWritten);
            auditQuestion = questionSize > 0;
            questionDirty = false;
        } else if (answerDirty) {
            writeText(answerVp, answer + answerOffset, answerSize - answerOffset, shownAnswer, answerWritten);
            answerDirty = false;
        } else if (graphSize) {
            if (graphOffset < graphSize) {
                const size_t count = std::min(size_t(36), graphSize - graphOffset);
                writeVp(uint16_t(graphVp + graphOffset), graph + graphOffset, count);
                graphOffset += count;
            } else {
                writeVp(graphVp, graph, 1);
                graphSize = 0;
                finishPreview(2);
            }
        }
    }
    void clearTouch() {
        const uint16_t zero = 0;
        writeVp(touchVp, &zero, 1);
    }
    void clearInteractiveDisplay() {
        if (assistantId) requestPc(assistantId, cancel);
        assistantId = 0;
        questionSize = 0;
        auditQuestion = false;
        finishAssistant(0);
        questionDirty = true;
        graphSize = 0;
        graphClear = true;
        finishPreview(0);
    }
    void panelLost() {
        if (!panelOnline) return;
        panelOnline = false;
        JDDgusNative::setPanelOnline(false);
        clearInteractiveDisplay();
        page = home;
        queuedPage = 0;
    }
    void resetUart(uint32_t now) {
        panelLost();
        drain = true;
        latchedKey = 0;
        waiting = Poll::None;
        serialCount = 0;
        serial.clear();
        questionWritten = answerWritten = false;
        pollAt = now;
    }
    void finishPanelHandshake(uint16_t currentPage, uint32_t now, bool canReadPage = false) {
        clearInteractiveDisplay();
        page = home;
        // Page 0 has its own timed splash; leave it alone on cold boot.
        queuedPage = currentPage == 0 || currentPage == home ? 0 : home;
        panelOnline = true;
        JDDgusNative::setPanelOnline(true);
        panelReplyAt = now;
        pageReadable = canReadPage;
        splashVisible = currentPage == 0;
        splashSince = now;
        pageCheckAt = now;
        waiting = Poll::None;
    }
    void serialReply(uint16_t vp, uint16_t first, uint16_t second, uint8_t count,
                     uint32_t now, const PanelState& s) {
        if (vp == stepVp && count == 2 && waiting == Poll::Selectors) {
            if (first != seenStep && first < 6 && submit({Action::Step, first})) seenStep = first;
            if (second != seenSpeed && second >= 1 && second <= 100 && submit({Action::Speed, second})) seenSpeed = second;
            // A rejected selector must not leave a different displayed step.
            if (first >= 6 || second < 1 || second > 100 ||
                (s.busy || s.owner)) {
                const uint16_t values[] = {s.step, s.speed};
                writeVp(stepVp, values, 2);
                seenStep = s.step;
                seenSpeed = s.speed;
            }
            waiting = keyboardPage() ? Poll::Keyboard : Poll::Touch;
            readVp(keyboardPage() ? keyboardVp : touchVp, 1);
            pollSince = now;
        } else if (vp == touchVp && count == 1 && waiting == Poll::Clear) {
            if (first == 0) {
                waiting = Poll::Page;
                readVp(currentPageVp, 1);
                pollSince = now;
            }
        } else if (vp == currentPageVp && count == 1 && waiting == Poll::Page) {
            finishPanelHandshake(first, now, true);
        } else if (vp == currentPageVp && count == 1 && waiting == Poll::OnlinePage) {
            panelReplyAt = now;
            waiting = Poll::None;
            if (first == 0 && !splashVisible) {
                // A quick DWIN reboot may resume UART before the watchdog notices.
                resetUart(now);
                return;
            }
            splashVisible = first == 0;
            if (splashVisible && uint32_t(now - splashSince) >= 2500) showPage(home);
            if (!queuedPage && first != page && (keyboardPage() || page == answerPage || page == sheetPage)) {
                // These pages can also leave locally while the controller is busy.
                clearInteractiveDisplay();
                page = home;
            }
        } else if (vp == questionVp && count == 2 && waiting == Poll::Question) {
            panelReplyAt = now;
            waiting = Poll::None;
            const bool matched = first == shownQuestion[0] && second == shownQuestion[1];
            logEvent({0, Decision::None, questionVp, 0, uint8_t(matched ? 0 : 7)}, now);
        } else if (vp == touchVp && count == 1 && panelOnline) {
            // STOP can arrive as a spontaneous upload as well as a poll response.
            if (first == 14) {
                if (latchedKey != 14) JDDgusNative::stop();
                latchedKey = 14;
                clearTouch();
            }
            if (waiting != Poll::Touch) return;
            panelReplyAt = now;
            pollAt = now;  // Leave a write window after the full round trip.
            waiting = Poll::None;
            if (first == 0) latchedKey = 0;
            else if (first != 14) {
                clearTouch();
                if ((first & 0xf000) == keyboardPrefix) {
                    if (keyboardPage()) typeKey(first & 0x0fff, now);
                    latchedKey = 0;
                    return;
                }
                if (!latchedKey || (first >= 20 && first <= 31 && first != latchedKey)) {
                    if (first == 22) testPc(now);
                    else if (!interactiveKey(first, now) && first <= 255) submit({Action::Button, first});
                }
                latchedKey = first;
            }
        } else if (vp == keyboardVp && count == 1 && waiting == Poll::Keyboard) {
            if (first) {
                const uint16_t zero = 0;
                writeVp(keyboardVp, &zero, 1);
                if (keyboardPage()) typeKey(first, now);
            }
            waiting = Poll::Touch;
            readVp(touchVp, 1);
            pollSince = now;
        }
    }
    void serialByte(uint8_t value, uint32_t now, const PanelState& s) {
        if (serialCount && uint32_t(now - serialByteAt) >= 100) serialCount = 0;
        serialByteAt = now;
        if (serialCount == 0 && value != 0x5a) return;
        if (serialCount == 1 && value != 0xa5) {
            serialCount = value == 0x5a ? 1 : 0;
            return;
        }
        serialIn[serialCount++] = value;
        if (serialCount == 3 && serialIn[2] != 3 && serialIn[2] != 6 && serialIn[2] != 8) {
            serialCount = value == 0x5a ? 1 : 0;
        } else if (serialCount >= 3 && serialCount == size_t(serialIn[2]) + 3) {
            const uint8_t count = serialIn[2] == 8 ? 2 : 1;
            if (serialIn[2] >= 6 && serialIn[3] == 0x83 && serialIn[6] == count)
                serialReply(word(serialIn + 4), word(serialIn + 7), count == 2 ? word(serialIn + 9) : 0, count, now, s);
            serialCount = 0;
        }
    }
    void displayStatus(const PanelState& s) {
        uint16_t data[16]{};
        auto position = [&](size_t index, int64_t microns) {
            const auto raw = uint32_t(int32_t(microns / 10));
            data[index] = uint16_t(raw >> 16);
            data[index + 1] = uint16_t(raw);
        };
        position(2, (int64_t(s.x) - (s.calibrated ? s.originX : 0)) * paperDirection);
        position(4, (int64_t(s.y) - (s.calibrated ? s.originY : 0)) * paperDirection);
        position(6, s.width);
        position(8, s.height);
        data[10] = s.busy;
        data[11] = progress;
        data[12] = s.sheet ? 2 : s.calibrated ? 1 : 0;
        data[15] = loaded;
        writeVp(statusVp, data, 16);
        const uint16_t result[] = {currentResult(s),
                                   uint16_t(s.ready ? 2 : 1), uint16_t(s.ready)};
        writeVp(resultVp, result, 3);
        // 0 is the display's default while it waits for the first MKS update.
        const uint16_t connection = interactivePc ? 3 : WiFi.isConnected() || WiFi.getMode() == WIFI_AP ? 2 : 1;
        writeVp(assistantVp + 2, &connection, 1);
    }
    void serviceUart(uint32_t now, const PanelState& s) {
        if (!uart->tryBeginBridgeIo()) return;
        const auto nextEpoch = uart->passthroughEpoch();
        if (nextEpoch != epoch) {
            epoch = nextEpoch;
            resetUart(now);
        }
        uint8_t bytes[64];
        const int count = uart_read(1, bytes, sizeof(bytes), 0);
        if (count < 0) {
            resetUart(now);
            uart->endBridgeIo();
            return;
        }
        if (drain) {
            if (count == 0) drain = false;
            uart->endBridgeIo();
            return;
        }
        for (int i = 0; i < count; ++i) serialByte(bytes[i], now, s);
        if (waiting != Poll::None && uint32_t(now - pollSince) >= (waiting == Poll::OnlinePage ? 100 : 250)) {
            // A touch-register reply already confirmed the link; PIC_Now is optional.
            if (waiting == Poll::Page) finishPanelHandshake(home, now);
            else waiting = Poll::None;
        }
        if (panelOnline && uint32_t(now - panelReplyAt) >= 500) {
            resetUart(now);
            uart->endBridgeIo();
            return;
        }
        if (waiting == Poll::None && !serial.size() && panelOnline && queuedPage) {
            serviceInteractiveDisplay();
        } else if (waiting == Poll::None && !serial.size() && panelOnline && pageReadable &&
            uint32_t(now - pageCheckAt) >= 400) {
            pageCheckAt = pollSince = now;
            readVp(currentPageVp, 1);
            waiting = Poll::OnlinePage;
        } else if (waiting == Poll::None && !serial.size() && uint32_t(now - pollAt) >= (keyboardPage() ? 70 : 100)) {
            pollAt = pollSince = now;
            if (!panelOnline) {
                clearTouch();
                const uint16_t zero = 0;
                writeVp(keyboardVp, &zero, 1);
                const uint16_t values[] = {s.step, s.speed};
                writeVp(stepVp, values, 2);
                seenStep = s.step;
                seenSpeed = s.speed;
                readVp(touchVp, 1);
                waiting = Poll::Clear;
            } else {
                readVp(stepVp, 2);
                waiting = Poll::Selectors;
            }
        }
        if (panelOnline && waiting == Poll::None && (questionDirty || answerDirty)) serviceInteractiveDisplay();
        if (panelOnline && waiting == Poll::None && !serial.size() && auditQuestion && keyboardPage()) {
            auditQuestion = false;
            waiting = Poll::Question;
            pollSince = now;
            readVp(questionVp, 2);
        }
        if (panelOnline && waiting == Poll::None && uint32_t(now - lastDisplay) >= 350 && serial.space() >= 58) {
            displayStatus(s);
            lastDisplay = now;
        }
        if (panelOnline && waiting == Poll::None) serviceInteractiveDisplay();
        if (serial.size()) {
            const int sent = fnc_uart_try_tx_chars(UART_NUM_1, reinterpret_cast<const char*>(serial.bytes + serial.start), serial.size());
            if (sent > 0) serial.consume(size_t(sent));
            else if (sent < 0) resetUart(now);
        }
        uart->endBridgeIo();
    }

public:
    explicit Panel(const char* name) : Module(name) {}
    void init() override {
        uart = config->_uarts[1];
        if (!uart || !uart->configured() || config->_uart_channels[1] || uart->_baud != 9600 ||
            std::strcmp(uart->_txd_pin.name(), "gpio.23") || std::strcmp(uart->_rxd_pin.name(), "gpio.33") ||
            uart->_dataBits != UartData::Bits8 || uart->_parity != UartParity::None || uart->_stopBits != UartStop::Bits1 ||
            !JDDgusNative::init()) {
            log_warn("JD DWIN local control unavailable: check the verified MKS V2.2 profile");
            return;
        }
        bootId = esp_random();
        if (!bootId) bootId = 1;
        epoch = uart->passthroughEpoch();
        enabled = true;
        startListener(millis());
    }
    void build_info(Channel& out) override {
        if (enabled)
            log_msg_to(out, "[JD-DGUS:2,PORT=8888,UART=1,TX=23,RX=33,BAUD=9600,LEASE_MS=3000,UI=1,UPTIME_MS="
                       << millis() << ",RESET_REASON=" << int(esp_reset_reason()) << "]");
    }
    void poll() override {
        if (!enabled) return;
        const uint32_t now = millis();
        if (probeResult == 11 && uint32_t(now - probeSince) >= 1500) finishProbe(13, now);
        else if (probeResult >= 12 && uint32_t(now - probeSince) >= 3500) probeResult = 0;
        if (assistantId && uint32_t(now - assistantAt) >= jobTimeout) {
            requestPc(assistantId, cancel);
            finishAssistant(6);
        }
        if (previewId && uint32_t(now - previewAt) >= previewTimeout) finishPreview(3);
        PanelState current{};
        const bool valid = snapshot(current);
        if (valid) serviceUart(now, current);
        serviceNetwork(now, current, valid);
    }
};

ModuleFactory::InstanceBuilder<Panel> __attribute__((init_priority(112))) jd_panel_module("jd_dgus_panel", true);
}
#endif
