#pragma once

#include <cstdint>

namespace WebUI {
    // Pure scheduling policy: no Wi-Fi, sockets, credentials or motor actions.
    // Owns station attempts while the radio is up. An associated client of
    // the fallback AP takes priority over STA scans on the shared radio.
    class StaFallbackRetry {
    public:
        enum class Action { None, Begin, Cancel };
        static constexpr uint32_t RetryIntervalMs = 15000;
        static constexpr uint32_t AttemptBudgetMs = 30000;

        Action poll(uint32_t now, bool enabled, bool connected, bool radioReady, bool apHasClients, bool stopped) {
            if (connected) {
                pending_ = false;
                initialized_ = true;
                lastAttempt_ = now;
                return Action::None;
            }
            if (!enabled || !radioReady || apHasClients || !stopped) {
                const bool cancel = pending_;
                pending_ = false;
                lastAttempt_ = now;
                if (!enabled || !radioReady) initialized_ = false;
                return cancel ? Action::Cancel : Action::None;
            }
            if (!initialized_) {
                initialized_ = true;
                lastAttempt_ = now;
                return Action::None;
            }
            if (pending_ && uint32_t(now - lastAttempt_) < AttemptBudgetMs) return Action::None;
            if (uint32_t(now - lastAttempt_) < RetryIntervalMs) return Action::None;
            lastAttempt_ = now;
            pending_ = true;
            return Action::Begin;
        }

        bool ownsStation() const { return pending_; }
        void reset() { initialized_ = pending_ = false; }

    private:
        uint32_t lastAttempt_ = 0;
        bool initialized_ = false, pending_ = false;
    };
}
