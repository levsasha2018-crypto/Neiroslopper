"""Explicit controller profiles; identifying a controller never changes its setup.

FluidNC's YAML is authoritative. Numeric entries in the returned dictionary are
an internal desktop compatibility view, not legacy $$ values sent to the board.
"""
import math
import re
import json
from pathlib import Path

UNO = "uno_grbl11"
DLC32 = "mks_dlc32_fluidnc"
PROFILE_LABELS = {UNO: "Arduino Uno / GRBL 1.1", DLC32: "MKS DLC32 / FluidNC"}
FLUIDNC_CONFIG_NAME = "JD_Plotter_DLC32_V2"
PROFILE_KEY = "_verified_machine_profile"


def load_selected_profile(directory):
    if directory is None:
        return UNO
    try:
        value = json.loads((Path(directory) / "machine-profile.json").read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or set(value) != {"version", "machine_profile"} or
                type(value["version"]) is not int or value["version"] != 1):
            return UNO
        return profile_id(value["machine_profile"])
    except (OSError, ValueError, TypeError):
        return UNO


def save_selected_profile(directory, selected):
    selected = profile_id(selected)
    if directory is None:
        return
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "machine-profile.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"version": 1, "machine_profile": selected}, indent=2), encoding="utf-8")
    temporary.replace(target)


def pen_settings_directory(directory, selected):
    if directory is None:
        return None
    return Path(directory) if profile_id(selected) == UNO else Path(directory) / DLC32


def profile_id(value=UNO):
    if value in PROFILE_LABELS:
        return value
    matches = [key for key, label in PROFILE_LABELS.items() if label == value]
    if matches:
        return matches[0]
    raise ValueError("Выберите поддерживаемую плату: Uno или MKS DLC32 / FluidNC.")


def app_profile(app):
    variable = getattr(app, "machine_profile", None)
    return profile_id(variable.get()) if variable is not None else UNO


def assistant_facts(selected):
    """Maintained selected-profile facts, not USB identification or telemetry."""
    selected = profile_id(selected)
    return {"machine_profile": selected,
            "controller_board": "MKS DLC32 V2.0_001" if selected == DLC32 else "Arduino Uno + CNC Shield V3",
            "controller_firmware": "FluidNC" if selected == DLC32 else "GRBL 1.1",
            "motor_socket_roles": "portal X+Z; carriage Y; Z is second X motor" if selected == DLC32 else
                                  "portal X+A; carriage Y; A clones X",
            "pen_output_profile": "GPIO4 HIGH -> NPN -> relay IN LOW; M8 power ON; M9 OFF" if selected == DLC32 else
                                  "CoolEn/A3; polarity must match Uno firmware",
            "holding_configuration": "stepping/idle_ms in config.yaml; UI checkbox disabled" if selected == DLC32 else
                                      "desktop holding checkbox / GRBL $1",
            "endstop_configuration": "two NC dry switches: upper portal X-negative GPIO36; left carriage Y-positive GPIO35; hard limits ON, homing OFF" if selected == DLC32 else
                                     "historical Uno profile: no endstops, homing OFF",
            "dwin_connection": "DWIN -> MKS UART GPIO23/33 -> LAN TCP port 8888 -> PC; motion TCP port 23" if selected == DLC32 else
                               "DWIN -> second Uno USB-UART -> PC -> motion controller; separate USB ports"}


def settings_ready(settings, selected=UNO):
    selected = profile_id(selected)
    if selected == UNO:
        # Imported lazily to retain one source of truth for the existing Uno.
        from controller import REQUIRED
        return all(settings.get(k) == v for k, v in REQUIRED.items())
    return (settings.get(PROFILE_KEY) == DLC32 and settings.get(13) == 0 and
            settings.get(20) == 0 and settings.get(21) == (0 if settings.get("_board_revision") == "2.2" else 1) and settings.get(22) == 0 and
            all(isinstance(settings.get(k), (int, float)) and
                math.isfinite(settings[k]) and settings[k] > 0
                for k in (100, 101, 110, 111, 120, 121)))


def fluidnc_identity(lines):
    return any(re.match(r"\[VER:\d+\.\d+\s+FluidNC\s+v\d+\.\d+", line, re.I) for line in lines)


def fluidnc_pen_profile(settings=None):
    if settings and settings.get("_board_revision") == "2.2":
        return {"backend": "fluidnc", "active_low": True, "continuous": True,
                "flood_pin": "gpio.32:low"}
    return {"backend": "fluidnc", "active_low": False, "continuous": True,
            "flood_pin": "gpio.4"}


def pen_matches(firmware, settings, selected=UNO):
    if profile_id(selected) == UNO:
        from pen_lift import firmware_profile
        return firmware == firmware_profile(settings.active_low, settings.continuous)
    return (isinstance(firmware, dict) and settings.continuous is True and
            settings.active_low is firmware.get("active_low") and
            firmware in (fluidnc_pen_profile(), fluidnc_pen_profile({"_board_revision": "2.2"})))


def fluidnc_configuration(lines, legacy=None):
    """Validate the prepared board topology, retaining measured axis values.

    Reject duplicate YAML keys (ordinary safe_load silently accepts them),
    startup macros and unexpected axes instead of trusting a display name.
    """
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("Повторяющееся поле в конфигурации FluidNC.")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    text = "\n".join(line for line in lines
                     if not line.startswith(("[MSG:", "[GC:", "[OPT:")))
    if len(text) > 120000:
        raise ValueError("Слишком большая конфигурация FluidNC.")
    try:
        config = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ValueError("Не удалось прочитать YAML FluidNC; нужен подготовленный config.yaml.") from exc
    if isinstance(config, dict) and config.get("name") == "JD_Plotter_DLC32_V22":
        return fluidnc_v22_configuration(config, legacy)
    if not isinstance(config, dict) or config.get("name") != FLUIDNC_CONFIG_NAME:
        raise ValueError("На MKS нужна подготовленная конфигурация JD_Plotter_DLC32_V2.")

    def fail(message):
        raise ValueError("Конфигурация MKS DLC32 не совпадает: " + message)

    if config.get("board") != "MKS DLC32 V2.0_001":
        fail("нужна ревизия MKS DLC32 V2.0_001")
    if config.get("stepping", {}).get("engine") != "I2S_STATIC":
        fail("нужен движок шагов I2S_STATIC")
    if config.get("start", {}).get("must_home", False) is not False:
        fail("must_home должен быть false: автоматическое базирование пока не настроено")
    if config.get("start", {}).get("check_limits") is not True:
        fail("start/check_limits должен быть true для двух NC-концевиков")
    uart = config.get("uart1")
    if (not isinstance(uart, dict) or type(uart.get("baud")) is not int or uart["baud"] != 115200 or
            str(uart.get("mode", "")).upper() != "8N1" or
            str(uart.get("txd_pin", "")).lower() != "gpio.23" or
            str(uart.get("rxd_pin", "")).lower() != "gpio.33" or
            any(str(uart.get(key, "NO_PIN")).upper() != "NO_PIN" for key in ("rts_pin", "cts_pin"))):
        fail("DWIN требует uart1: 115200, 8N1, TX GPIO23, RX GPIO33, без RTS/CTS")
    if "uart_channel1" in config:
        fail("uart_channel1 должен отсутствовать: двоичные данные DWIN нельзя подавать в GCode")
    axes = config.get("axes")
    if not isinstance(axes, dict):
        fail("не найдены оси")
    def active_motor(motor):
        if not isinstance(motor, dict):
            return bool(motor)
        passive = {"null_motor", "limit_neg_pin", "limit_pos_pin", "limit_all_pin", "hard_limits",
                   "pulloff_mm", "limit_neg_pin", "limit_pos_pin"}
        return bool(set(motor) - passive) or any(
            str(motor.get(key, "NO_PIN")).upper() != "NO_PIN"
            for key in ("limit_neg_pin", "limit_pos_pin", "limit_all_pin"))
    for name in ("z", "a", "b", "c"):
        # FluidNC's serializer materializes at least a default third axis.
        # A Z/null_motor is harmless; a real independent Z driver is not.
        axis = axes.get(name) or {}
        if not isinstance(axis, dict) or any(active_motor(motor) for key, motor in axis.items()
                                            if re.fullmatch(r"motor\d+", key)):
            fail("канал Z должен быть вторым мотором X, а не отдельной осью")
    # DLC32 V2.0_001: physical Z socket is the mirrored second X motor.
    expected = {"x": (("i2so.1", "i2so.2"), ("i2so.3", "i2so.4:low")),
                "y": (("i2so.5", "i2so.6"),)}
    i2s = config.get("i2so", {})
    if tuple(str(i2s.get(key, "")).lower() for key in ("bck_pin", "ws_pin", "data_pin")) != (
            "gpio.16", "gpio.17", "gpio.21"):
        fail("контакты I2S должны соответствовать DLC32 V2.0")
    if str(axes.get("shared_stepper_disable_pin", "")).lower() not in ("i2so.0", "i2so.0:high"):
        fail("общий ENABLE драйверов должен быть i2so.0")
    result = dict(legacy or {})
    result.pop(PROFILE_KEY, None)
    for index, name in enumerate(("x", "y")):
        axis = axes.get(name)
        if not isinstance(axis, dict):
            fail("отсутствует ось " + name.upper())
        homing = axis.get("homing")
        if (axis.get("soft_limits", False) is not False or
                (homing is not None and (not isinstance(homing, dict) or
                 type(homing.get("cycle")) is not int or homing["cycle"] >= 0))):
            fail("требуется ручная установка углов без homing и soft limits")
        for source, destination in (("steps_per_mm", 100), ("max_rate_mm_per_min", 110),
                                    ("acceleration_mm_per_sec2", 120)):
            number = axis.get(source)
            if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                fail("проверьте " + name + "/" + source)
            result[destination + index] = float(number)
        for motor_index, (step, direction) in enumerate(expected[name]):
            motor = axis.get("motor" + str(motor_index), {})
            driver = motor.get("stepstick", {})
            def normalized(pin):
                value = str(pin).lower()
                return value[:-5] if value.endswith(":high") else value
            if normalized(driver.get("step_pin")) != step or normalized(driver.get("direction_pin")) != direction:
                fail(f"контакты {name}/motor{motor_index}")
            desired_limits = ({"limit_neg_pin": "gpio.36"} if name == "x" and motor_index == 0 else
                              {"limit_pos_pin": "gpio.35"} if name == "y" else {})
            for key in ("limit_neg_pin", "limit_pos_pin", "limit_all_pin"):
                pin = normalized(motor.get(key, "NO_PIN"))
                if pin != desired_limits.get(key, "no_pin"):
                    fail("NC-концевик: верх портала GPIO36/X−; левый край GPIO35/Y+; остальные входы NO_PIN")
            if motor.get("hard_limits", False) is not bool(desired_limits):
                fail("hard_limits должны быть включены у x/motor0 и y/motor0")
        if any(active_motor(motor) for key, motor in axis.items()
               if re.fullmatch(r"motor\d+", key) and int(key[5:]) >= (2 if name == "x" else 1)):
            fail("лишний мотор оси " + name)
    coolant = config.get("coolant", {})
    if str(coolant.get("flood_pin", "")).lower() not in ("gpio.4", "gpio.4:high"):
        fail("выход M8/M9 должен быть gpio.4 HIGH для транзисторного согласования реле")
    if str(coolant.get("mist_pin", "NO_PIN")).upper() != "NO_PIN":
        fail("выход mist должен быть отключён")
    for key, value in (config.get("macros") or {}).items():
        if key.startswith(("startup", "after_homing")) and value not in (None, ""):
            fail("автоматический макрос запуска не должен содержать команд")
    # FluidNC has no legacy $13 proxy: its YAML setting is authoritative.
    if config.get("report_inches") is not False:
        fail("report_inches должен быть false: координаты только в миллиметрах")
    if any(key in result and result[key] != expected for key, expected in ((20, 0), (21, 1), (22, 0))):
        fail("нужны hard limits включены ($21=1), homing и soft limits выключены")
    result.update({PROFILE_KEY: DLC32, "_dwin_uart_baud": 115200, 13: 0, 20: 0, 21: 1, 22: 0,
                   1: config.get("stepping", {}).get("idle_ms", 0)})
    return result


def fluidnc_v22_configuration(config, legacy=None):
    """Specific current V2.2 wiring, read from this physical board, not V2.0.

    Existing manual-test kinematics, steps and relay are retained. Limits
    remain explicitly disabled until the physical NC switches are checked.
    """
    def require(condition, message):
        if not condition:
            raise ValueError("Конфигурация MKS V2.2: " + message)
    for section in ("stepping", "uart1", "i2so", "axes", "coolant", "start", "macros"):
        require(isinstance(config.get(section), dict),
                "ответ неполный или повреждён: нет раздела " + section)
    require("report_inches" in config, "ответ неполный: нет поля report_inches")
    automatic_macros = ("startup_line0", "startup_line1", "after_homing", "after_reset", "after_unlock")
    require(all(key in config["macros"] for key in automatic_macros),
            "ответ неполный: не прочитаны автоматические макросы")
    require(config.get("board") == "MKS DLC32 v2.2", "не совпадает версия платы")
    require(config.get("stepping", {}).get("engine") in ("I2S", "I2S_STATIC"), "нужен I2S_STATIC")
    require(config.get("report_inches") is False, "нужны миллиметры")
    uart = config.get("uart1", {})
    require(str(uart.get("txd_pin", "")).lower() == "gpio.23" and
            str(uart.get("rxd_pin", "")).lower() == "gpio.33" and
            type(uart.get("baud")) is int and uart.get("baud") == 9600 and
            str(uart.get("mode", "")).upper() == "8N1" and
            all(str(uart.get(k, "NO_PIN")).upper() == "NO_PIN" for k in ("rts_pin", "cts_pin")),
            "проверенный DWIN требует TX23/RX33/9600/8N1, без RTS/CTS")
    require("uart_channel1" not in config, "UART дисплея не должен принимать G-code")
    axes = {str(k).lower(): v for k, v in config.get("axes", {}).items()}
    require(len(axes) == len(config["axes"]), "повтор оси с разным регистром имени")
    require(str(axes.get("shared_stepper_disable_pin", "")).lower() == "i2so.0", "ENABLE должен быть I2SO.0")
    i2s = config.get("i2so", {})
    require(tuple(str(i2s.get(k, "")).lower() for k in ("bck_pin", "ws_pin", "data_pin")) ==
            ("gpio.16", "gpio.17", "gpio.21"), "не совпадает шина I2S")
    expected = {"x": (("i2so.5", "i2so.6"),),
                "y": (("i2so.1", "i2so.2"), ("i2so.3", "i2so.4:low"))}
    result = dict(legacy or {})
    for index, name in enumerate(("x", "y")):
        axis = axes.get(name, {})
        require(axis.get("soft_limits", False) is False and not axis.get("homing"), "базирование не настроено")
        for field, dest in (("steps_per_mm", 100), ("max_rate_mm_per_min", 110), ("acceleration_mm_per_sec2", 120)):
            value = axis.get(field)
            require(type(value) in (int, float) and math.isfinite(value) and value > 0, "неверное значение " + field)
            result[dest + index] = float(value)
        motors = {k: v for k, v in axis.items() if re.fullmatch(r"motor\d+", k)}
        require(set(motors) == {"motor" + str(i) for i in range(len(expected[name]))}, "не совпадает число моторов " + name)
        for i, (step, direction) in enumerate(expected[name]):
            motor = motors["motor" + str(i)]
            driver = motor.get("stepstick", {})
            require(str(driver.get("step_pin", "")).lower() == step and
                    str(driver.get("direction_pin", "")).lower() == direction,
                    "не совпадают контакты мотора " + name)
            require(not motor.get("hard_limits") and all(str(motor.get(k, "NO_PIN")).upper() == "NO_PIN"
                    for k in ("limit_neg_pin", "limit_pos_pin", "limit_all_pin")), "профиль концевиков требует отдельной настройки")
    for name in ("z", "a", "b", "c"):
        axis = axes.get(name, {})
        require(not any("stepstick" in v for k, v in axis.items() if re.fullmatch(r"motor\d+", k)), "найдена лишняя реальная ось")
    require(str(config.get("coolant", {}).get("flood_pin", "")).lower() == "gpio.32:low", "реле должно быть GPIO32 LOW")
    require(str(config.get("coolant", {}).get("mist_pin", "NO_PIN")).upper() == "NO_PIN", "лишний выход mist")
    require(config.get("start", {}).get("must_home", False) is False and
            config.get("start", {}).get("check_limits", False) is False, "не совпадает режим ручной калибровки")
    require(all(value in (None, "") for key, value in (config.get("macros") or {}).items()
                if key.startswith(("startup", "after_"))), "есть автоматические макросы")
    pulse = config["stepping"].get("pulse_us", 4)
    frame = i2s.get("min_pulse_us", 2)
    require(type(pulse) is int and 0 <= pulse <= 30 and type(frame) is int and frame in (1, 2, 4),
            "неверная длительность импульсов I2S")
    # Match the ESP32 I2S engine's rounding to whole frames and its
    # minimum OFF time. This bounds the pulse generator, not motor torque.
    pulse_frames = math.ceil(min(max(pulse, frame), 20) / frame)
    step_hz = 1_000_000 // (2 * pulse_frames * frame)
    result.update({PROFILE_KEY: DLC32, "_board_revision": "2.2", "_axis_layout": "x_carriage_y_portal",
                   "_xy_pulse_feed_max": step_hz * 60 / max(result[100], result[101]),
                   "_dwin_uart_baud": 9600,
                   13: 0, 20: 0, 21: 0, 22: 0, 1: config.get("stepping", {}).get("idle_ms", 100)})
    return result
