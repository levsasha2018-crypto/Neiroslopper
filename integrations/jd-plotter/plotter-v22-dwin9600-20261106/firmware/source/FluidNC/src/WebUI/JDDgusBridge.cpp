#ifndef JD_DGUS_NATIVE
// Jerusalem Dynamics: bounded binary DGUS tunnel. FluidNC CNC engine unchanged.
// GPL-3.0-or-later, same license as FluidNC. No GPIO writes occur here.
// TCP/8888: 'J' 'D' version=1 type payload_length_be16 payload.
#include "Module.h"
#include "Machine/MachineConfig.h"
#include "Protocol.h"
#include "Report.h"
#include "Uart.h"
#include "Driver/fluidnc_uart.h"
#include <driver/uart.h>
#include "fnc_idf_uart.h"
#include <Arduino.h>
#include <esp_system.h>
#include <lwip/sockets.h>
#include <lwip/tcp.h>
#include <fcntl.h>
#include <errno.h>
#include <cstring>
#include <algorithm>
#include <freertos/queue.h>

// The existing CNC task consumes this queue. Never execute reset on the IO task.
extern QueueHandle_t event_queue;

namespace {
constexpr uint16_t PORT = 8888;
constexpr size_t MAX_PAYLOAD = 512;
constexpr uint32_t LEASE_MS = 3000;
constexpr uint8_t DATA_TO_UART = 0x01, DATA_TO_PC = 0x02;
constexpr uint8_t LEASE = 0x10, ACK = 0x11, HELLO = 0x7f;

template <size_t N> class ByteQueue {
    uint8_t data[N];
    size_t head = 0, length = 0;
public:
    size_t size() const { return length; }
    size_t space() const { return N - length; }
    void clear() { head = length = 0; }
    bool append(const uint8_t* bytes, size_t count) {
        if (count > space()) return false;
        for (size_t i = 0; i < count; ++i) data[(head + length + i) % N] = bytes[i];
        length += count;
        return true;
    }
    const uint8_t* front() const { return data + head; }
    size_t contiguous() const { return std::min(length, N - head); }
    void consume(size_t count) { head = (head + count) % N; length -= count; }
};

class JDDgusBridge : public Module {
    int listener = -1, peer = -1;
    bool enabled = false, armed = false, resetPending = false;
    uint32_t lastHeartbeat = 0;
    uint32_t lastTrip = 0;
    Uart* uart = nullptr;
    uint32_t uartEpoch = 0;
    bool uartNeedsDrain = false;
    ByteQueue<4096> toNetwork;
    ByteQueue<4096> toUart;
    uint8_t input[6 + MAX_PAYLOAD] = {};
    size_t inputLength = 0, wanted = 6;

    bool frame(uint8_t type, const uint8_t* data, size_t count) {
        if (count > MAX_PAYLOAD || count + 6 > toNetwork.space()) return false;
        uint8_t header[] = {'J', 'D', 1, type, uint8_t(count >> 8), uint8_t(count)};
        return toNetwork.append(header, 6) && toNetwork.append(data, count);
    }
    void discardUartInput() {
        if (!uart || !uart->tryBeginBridgeIo()) return;
        uint8_t discarded[256];
        // uart_read's zero timeout also bounds acquisition of the RX mutex.
        uart_read(1, discarded, sizeof(discarded), 0);
        uart->endBridgeIo();
    }
    bool uartWasServiced() {
        const auto epoch = uart->passthroughEpoch();
        const bool changed = epoch != uartEpoch;
        if (changed || uart->passthroughActive()) toUart.clear();
        if (changed) uartNeedsDrain = true;
        uartEpoch = epoch;
        return changed;
    }
    void enqueueReset() {
        if (!resetPending) return;
        EventItem item { &rtResetEvent, nullptr };
        // A full event queue cannot silently lose the stop. Retry next poll.
        if (xQueueSendToFront(event_queue, &item, 0) == pdTRUE) resetPending = false;
    }
    void tripLease() {
        if (!armed) return;
        armed = false;
        lastTrip = millis();
        resetPending = true;
        toUart.clear();
        discardUartInput();
        enqueueReset();
        log_warn("JD DGUS control lease released/lost; queued CNC reset");
    }
    void disconnect(bool failSafe, const char* reason) {
        log_warn("JD DGUS disconnect: " << reason);
        if (failSafe) tripLease();
        armed = false;
        if (peer >= 0) { shutdown(peer, SHUT_RDWR); close(peer); peer = -1; }
        inputLength = 0;
        wanted = 6;
        toNetwork.clear();
        toUart.clear();
        discardUartInput();
    }
    bool acceptFrame(uint32_t now) {
        uint16_t length = (uint16_t(input[4]) << 8) | input[5];
        if (input[3] == DATA_TO_UART) {
            // Console service owns UART exclusively. Never replay UI writes
            // queued before/during that service when its serial mode returns.
            const bool serviced = uartWasServiced();
            if (serviced || uart->passthroughActive()) return length != 0;
            return length && toUart.append(input + 6, length);
        }
        if (input[3] == LEASE && length == 1 && input[6] <= 1) {
            if (!input[6]) tripLease();
            else if (!resetPending && (!lastTrip || uint32_t(now - lastTrip) >= 1000)) armed = true;
            lastHeartbeat = now;
            const uint8_t response[] = { uint8_t(armed), uint8_t(resetPending ? 1 : 0) };
            return frame(ACK, response, sizeof(response));
        }
        return false;
    }
    bool consumeByte(uint8_t value, uint32_t now) {
        input[inputLength++] = value;
        if (inputLength == 6) {
            if (input[0] != 'J' || input[1] != 'D' || input[2] != 1) return false;
            uint16_t length = (uint16_t(input[4]) << 8) | input[5];
            if (length > MAX_PAYLOAD) return false;
            wanted = 6 + length;
        }
        if (inputLength == wanted) {
            bool result = acceptFrame(now);
            inputLength = 0;
            wanted = 6;
            return result;
        }
        return true;
    }
public:
    explicit JDDgusBridge(const char* name) : Module(name) {}
    void init() override {
        uart = config->_uarts[1];
        if (!uart || !uart->configured() || config->_uart_channels[1]) {
            log_warn("JD DGUS disabled: configure uart1, but NO uart_channel1");
            return;
        }
        if ((uart->_baud != 9600 && uart->_baud != 115200) || strcmp(uart->_txd_pin.name(), "gpio.23") || strcmp(uart->_rxd_pin.name(), "gpio.33") ||
            uart->_dataBits != UartData::Bits8 || uart->_parity != UartParity::None || uart->_stopBits != UartStop::Bits1) {
            log_config_error("JD DGUS needs uart1 9600 or 115200 8N1 TX=gpio.23 RX=gpio.33");
            return;
        }
        listener = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (listener < 0) { log_warn("JD DGUS socket unavailable"); return; }
        int on = 1;
        setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
        if (fcntl(listener, F_SETFL, O_NONBLOCK) < 0) {
            close(listener); listener = -1;
            log_warn("JD DGUS cannot make listener nonblocking"); return;
        }
        sockaddr_in address = {};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_ANY);
        address.sin_port = htons(PORT);
        if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) || listen(listener, 1)) {
            close(listener); listener = -1;
            log_warn("JD DGUS port 8888 unavailable"); return;
        }
        enabled = true;
        uartEpoch = uart->passthroughEpoch();
        log_info("JD-DGUS/1 TCP 8888 UART1 TX23 RX33 baud=" << uart->_baud << " lease=3000ms");
    }
    void build_info(Channel& out) override {
        log_stream(out, "[JD-DGUS:1,PORT=8888,UART=1,TX=23,RX=33,BAUD=" << (uart ? uart->_baud : 0)
                   << ",LEASE_MS=3000,UPTIME_MS=" << millis() << ",RESET_REASON=" << int(esp_reset_reason()) << "]");
    }
    void poll() override {
        if (!enabled) return;
        enqueueReset();
        const uint32_t now = millis();
        // Check before handling queued packets: an overdue lease cannot be revived.
        if (peer >= 0 && armed && uint32_t(now - lastHeartbeat) > LEASE_MS) {
            disconnect(true, "control heartbeat timeout");
            return;
        }
        int newcomer = accept(listener, nullptr, nullptr);
        if (newcomer >= 0) {
            if (peer >= 0) { close(newcomer); }
            else {
                peer = newcomer;
                if (fcntl(peer, F_SETFL, O_NONBLOCK) < 0) {
                    disconnect(false, "cannot make client nonblocking");
                    return;
                }
                int on = 1;
                setsockopt(peer, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));
                armed = false;
                lastHeartbeat = now;
                inputLength = 0; wanted = 6;
                toNetwork.clear(); toUart.clear(); discardUartInput();
                static const uint8_t hello[] = "JD-DGUS/1";
                frame(HELLO, hello, sizeof(hello) - 1);
            }
        }
        uartWasServiced();
        if (peer < 0) { discardUartInput(); return; }
        if (!armed && uint32_t(now - lastHeartbeat) > 10000) { disconnect(false, "inactive client timeout"); return; }
        uint8_t buffer[256];
        int got = recv(peer, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (got == 0) { disconnect(true, "client closed connection"); return; }
        if (got < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { disconnect(true, "socket receive error"); return; }
        for (int i = 0; i < got; ++i) {
            if (!consumeByte(buffer[i], now)) { disconnect(true, "invalid frame or UART queue overflow"); return; }
        }
        // Network framing and the control lease keep running during $UP, but
        // no bridge read/write/flush may consume the service UART's bytes.
        if (uart->tryBeginBridgeIo()) {
            uartWasServiced();
            int sent = 0;
            int bytes = 0;
            bool overflow = false;
            if (uartNeedsDrain) {
                // Drain service replies BEFORE sending any new DGUS request.
                // One zero-timeout read per poll; leave network/lease running
                // until a later empty read proves the UART input is drained.
                bytes = uart_read(1, buffer, sizeof(buffer), 0);
                if (bytes == 0) uartNeedsDrain = false;
            } else {
                if (toUart.size()) {
                    sent = fnc_uart_try_tx_chars(UART_NUM_1, reinterpret_cast<const char*>(toUart.front()),
                                                std::min(toUart.contiguous(), size_t(128)));
                    if (sent > 0) toUart.consume(size_t(sent));
                }
                bytes = uart_read(1, buffer, sizeof(buffer), 0);
                overflow = bytes > 0 && !frame(DATA_TO_PC, buffer, size_t(bytes));
            }
            uart->endBridgeIo();
            if (sent < 0 || bytes < 0) { disconnect(true, "UART driver error"); return; }
            if (overflow) { disconnect(true, "network output queue overflow"); return; }
        }
        if (toNetwork.size()) {
            int sent = send(peer, toNetwork.front(), std::min(toNetwork.contiguous(), size_t(512)), MSG_DONTWAIT);
            if (sent > 0) toNetwork.consume(size_t(sent));
            else if (sent == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                disconnect(true, "socket send error"); return;
            }
        }
    }
};

ModuleFactory::InstanceBuilder<JDDgusBridge> __attribute__((init_priority(112))) jd_dgus_module("jd_dgus_bridge", true);
}  // namespace
#endif
