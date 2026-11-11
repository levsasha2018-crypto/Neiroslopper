"""Read-only commissioning check for this MKS/DWIN installation."""
import argparse
from contextlib import closing
import ipaddress
import json
from pathlib import Path
import re
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'PROGRAM'))
from controller import GrblSession, dgus_marker
from board_discovery import board_address, discover_board
from dgus_tunnel import HELLO, frame
from dwin_native import STATE, STATE_STRUCT, NativeState
from machine_profiles import DLC32, settings_ready
from network_transport import TcpWire

def receive(wire, size, deadline):
    received = bytearray()
    while len(received) < size and time.monotonic() < deadline:
        received.extend(wire.read(size - len(received)))
    if len(received) != size:
        raise TimeoutError('MKS не прислала полный ответ на порту DWIN.')
    return bytes(received)


def panel_status(ip, version):
    with closing(TcpWire(f'tcp://{ip}:8888')) as wire:
        deadline = time.monotonic() + 3
        expected = frame(HELLO, f'JD-DGUS/{version}'.encode('ascii'))
        if receive(wire, len(expected), deadline) != expected:
            raise ValueError('Порт 8888 вернул неожиданный протокол экрана.')
        if version == 1:
            return None  # JD-DGUS/1 hello does not attest UART traffic.
        for _ in range(16):
            header = receive(wire, 6, deadline)
            length = int.from_bytes(header[4:6], 'big')
            if header[:3] != b'JD\x01' or length > 512:
                raise ValueError('Повреждённый пакет протокола DWIN.')
            payload = receive(wire, length, deadline)
            if header[3] == STATE and length == STATE_STRUCT.size:
                return NativeState.decode(payload, time.monotonic()).panel_online
        raise TimeoutError('MKS открыла порт 8888, но не передала состояние экрана.')


def verify_wifi(ip):
    with closing(TcpWire(f'tcp://{ip}:23')) as wire:
        session = GrblSession(wire, threading.Event(), lambda *_: None, DLC32)
        info = session.command('$I')
        address, mode = board_address(info, peer=ip)
        session.refresh_settings()
        if not settings_ready(session.settings, DLC32) or session.settings.get('_board_revision') != '2.2':
            raise ValueError('MKS ответила, но профиль контроллера не готов.')
    marker = dgus_marker(info)
    if ',BAUD=9600,' not in marker or not re.match(r'\[JD-DGUS:[12],', marker):
        raise ValueError('Профиль экрана MKS должен использовать JD-DGUS и 9600 бод.')
    version = int(marker[len('[JD-DGUS:')])
    print(f'MKS доступна по {ip}:23, режим {mode}, настройки V2.2 подтверждены.')
    panel = panel_status(ip, version)
    print(f'Порт DWIN 8888 отвечает по JD-DGUS/{version}.')
    if panel is True:
        print('MKS видит ответ физического экрана по UART.')
    elif panel is False:
        print('Экран пока не отвечает по UART. Проверьте питание, разъём, RX/TX и загрузку DWIN_SET.')
    else:
        print('Связь с самим экраном нужно проверить в пульте.')
    return panel


def usb_address(requested_port):
    from serial import Serial
    from serial.tools.list_ports import comports

    ports = [item.device for item in comports() if (item.vid, item.pid) == (0x1A86, 0x7523)]
    if requested_port:
        ports = [port for port in ports if port.casefold() == requested_port.casefold()]
    if len(ports) != 1:
        raise ValueError('Нужен один USB-порт MKS (CH340). Найдено: ' + (', '.join(ports) or 'ни одного'))
    wire = Serial(port=None, baudrate=115200, timeout=.2, write_timeout=.5)
    wire.dtr = wire.rts = False
    wire.port = ports[0]
    with wire:
        print(f'Проверяю MKS через {wire.port}.')
        for _ in range(3):
            wire.reset_input_buffer()
            wire.write(b'\n$I\n')
            deadline = time.monotonic() + 2
            lines = []
            while time.monotonic() < deadline:
                line = wire.read_until(size=1024).decode('utf-8', 'replace').strip()
                if line:
                    lines.append(line)
                if line == 'ok' and any(x.startswith('[VER:') for x in lines):
                    return board_address(lines)
    raise TimeoutError('CH340 виден, но ESP32 не отвечает. Нужен журнал запуска с отключённым DWIN.')


def remember(file, ip):
    saved = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
    saved.update(host=ip, station_ip=ip, motion=f'tcp://{ip}:23', dwin=f'tcp://{ip}:8888', dwin_baud=9600)
    pending = file.with_suffix('.tmp')
    pending.write_text(json.dumps(saved, indent=2) + '\n', encoding='utf-8')
    pending.replace(file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usb', metavar='COM', nargs='?', const='', help='найти актуальный IP через USB')
    args = parser.parse_args()
    config = ROOT / 'connection.json'
    try:
        if args.usb is None:
            saved = json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
            ip = str(ipaddress.IPv4Address(saved.get('host', '192.168.0.1')))
            found = discover_board(ip)
            if found is None:
                raise OSError('Плата пока не обнаружена в локальной сети.')
            changed = found != ip
            ip = found
        else:
            print('Закройте пульт. Для проверки через USB отключите питание моторов и соленоида.')
            if input('Оба БП выключены и пульт закрыт? [y/N] ').strip().casefold() not in ('y', 'д', 'да'):
                return 1
            ip, mode = usb_address(args.usb)
            print(f'MKS сообщила адрес {ip} в режиме {mode}.')
            if mode == 'AP':
                print('В режиме AP ноутбук должен быть подключён к точке доступа FluidNC.')
        if not ipaddress.IPv4Address(ip).is_private:
            raise ValueError('Ожидается локальный IP платы.')
        panel = verify_wifi(ip)
        if args.usb is not None or changed or not config.exists():
            remember(config, ip)
            print('Адрес проверен и сохранён для START.cmd.')
        return 1 if panel is False else 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f'Проверка остановилась. {exc}')
        if args.usb is None:
            print('Если адрес сменился, закройте пульт и запустите CHECK_SETUP.cmd --usb.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
