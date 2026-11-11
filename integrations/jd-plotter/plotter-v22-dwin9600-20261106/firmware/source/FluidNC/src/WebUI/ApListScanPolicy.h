#pragma once

namespace WebUI {
    // Only an explicit list request starts a scan. STA may be borrowed while
    // scanning in AP-only mode; its cleanup must not own any real connection.
    class ApListScanPolicy {
    public:
        enum class Action { None, BeginScan, DisableStation };

        Action start(bool noResultsOrScan, bool apOnly) {
            if (!noResultsOrScan) return Action::None;
            borrowedStation_ = apOnly;
            return Action::BeginScan;
        }

        Action finish(bool retryOwnsStation, bool connected, bool apSta) {
            const bool disable = borrowedStation_ && !retryOwnsStation && !connected && apSta;
            borrowedStation_ = false;
            return disable ? Action::DisableStation : Action::None;
        }

        Action poll(bool scanDone, bool retryOwnsStation, bool connected, bool apSta) {
            return scanDone ? finish(retryOwnsStation, connected, apSta) : Action::None;
        }

        void reset() { borrowedStation_ = false; }

    private:
        bool borrowedStation_ = false;
    };
}
