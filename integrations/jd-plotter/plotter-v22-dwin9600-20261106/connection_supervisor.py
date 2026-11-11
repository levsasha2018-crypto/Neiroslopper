"""Reconnect the transport from the launcher. Never submit an operation."""
import queue
import threading
import time

from board_discovery import discover_board
from machine_profiles import DLC32, PROFILE_LABELS
from network_transport import parse_tcp_endpoint


class ConnectionSupervisor:
    def __init__(self, app, endpoint, *, discover=discover_board, clock=time.monotonic):
        self.app = app
        self.endpoint = endpoint
        self.discover = discover
        self.clock = clock
        self.results = queue.SimpleQueue()
        self.running = False
        self.active = True
        self.next_attempt = 0.0
        self.backoff = 1.0

    def pause(self):
        self.active = False

    def poll(self):
        app = self.app
        if (not self.active or app.closed or app.port.get() != self.endpoint or
                app.machine_profile.get() != PROFILE_LABELS[DLC32]):
            self.pause()
            return
        if app.connected:
            self.backoff = 1.0
            return
        if app.controller.busy or not app.controller.events.empty():
            return
        if self.running:
            try:
                address = self.results.get_nowait()
            except queue.Empty:
                return
            self.running = False
            self.next_attempt = self.clock() + self.backoff
            self.backoff = min(6.0, self.backoff * 2)
            if address:
                self.endpoint = f'tcp://{address}:23'
                app.port.set(self.endpoint)
                app.submit('connect', port=self.endpoint, hold=app.hold.get(),
                           machine_profile=DLC32, expected_board=True)
            return
        if self.clock() < self.next_attempt:
            return
        host = parse_tcp_endpoint(self.endpoint).host
        self.running = True
        app.status.set('Ищу MKS в локальной сети…')

        def worker():
            address = None
            try:
                address = self.discover(host)
            except (OSError, ValueError):
                pass
            finally:
                self.results.put(address)

        threading.Thread(target=worker, daemon=True, name='MKS discovery').start()
