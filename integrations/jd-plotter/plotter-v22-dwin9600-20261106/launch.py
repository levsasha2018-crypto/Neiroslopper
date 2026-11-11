"""Open V2.2 + the physically verified 9600-baud DWIN; no motion commands."""
import json
from pathlib import Path
import sys
import time
import tkinter as tk

ROOT = Path(__file__).resolve().parent
program = ROOT / "PROGRAM"
sys.path.insert(0, str(program))
log = open(ROOT / "app-runtime.log", "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log
from app import App
from connection_supervisor import ConnectionSupervisor
from machine_profiles import DLC32, PROFILE_LABELS

root = tk.Tk()
app = App(root, data_dir=program / "data")
app.machine_profile.set(PROFILE_LABELS[DLC32])
connection = {"motion": "tcp://192.168.0.1:23"}
try:
    if (ROOT / "connection.json").exists():
        saved = json.loads((ROOT / "connection.json").read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or ("motion" in saved and not isinstance(saved["motion"], str)):
            raise ValueError("Некорректный формат адресов")
        connection.update(saved)
except (OSError, ValueError):
    connection["motion"] = "DEMO"
    app.note("Не удалось прочитать адреса из connection.json. Открыт режим DEMO.")
app.port.set(connection["motion"])
app.refresh_ports()
app.root.title("Плоттер · MKS V2.2 · DWIN 9600")
app.root.state("zoomed")
app.hint.set("MKS V2.2: X — каретка, Y — портал. Концевиков пока нет; лист задаётся двумя углами.")
supervisor = ConnectionSupervisor(app, connection['motion'])
if not connection['motion'].startswith('tcp://'):
    supervisor.pause()

def manual_connect():
    if not app.connected and app.port.get().startswith('tcp://') and not app.controller.busy:
        supervisor.endpoint = app.port.get()
        supervisor.active = True
        supervisor.next_attempt = 0
        supervisor.poll()
    else:
        supervisor.pause()
        app.connect()

app.connect_button.configure(command=manual_connect)

def start_connections():
    if app.closed or not supervisor.active:
        return
    supervisor.poll()
    if app.connected and app.port_name.startswith('tcp://') and connection["motion"] != app.port_name:
        from network_transport import parse_tcp_endpoint
        host = parse_tcp_endpoint(app.port_name).host
        connection.update(host=host, station_ip=host, motion=app.port_name, dwin=f"tcp://{host}:8888")
        temporary = ROOT / "connection.tmp"
        temporary.write_text(json.dumps(connection, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(ROOT / "connection.json")
    root.after(400, start_connections)

def save_health():
    if app.closed:
        return
    bridge = app.dwin_window.bridge if app.dwin_window else None
    native = app.panel_client()
    panel_state = native.snapshot() if native else None
    age = time.monotonic() - bridge.last_panel_response if bridge and not native else float("inf")
    status = {"connected": app.connected, "controller": app.port_name,
              "profile": app.machine_profile.get(), "settings_ready": app.machine_ready(),
              "dwin_tunnel_connected": bool(bridge and bridge.connected.is_set()),
              "dwin_replied": panel_state.panel_online if panel_state else age < 2, "status": app.status.get(),
              "pen_ready": app.pen_ready(),
              "screen_commands_enabled": (panel_state.confirmed and panel_state.panel_online if panel_state else
                                          bool(app.dwin_window and app.dwin_window.armed.get())),
              "dwin_status": app.dwin_window.info.get() if app.dwin_window else "",
              "no_motion_requested": True}
    (ROOT / "app-health.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    root.after(2000, save_health)

root.after(400, start_connections)
root.after(2500, save_health)
root.mainloop()
