"""Validated A3 relay profile and deterministic, duty-limited drawing batches.

Physical motion is configurable independently of electrical relay polarity.
M8 energizes and M9 de-energizes in either matching firmware polarity.
This is not a temperature sensor or a guarantee of safe solenoid duty cycle.
"""
from dataclasses import dataclass, asdict
import json
import math
from pathlib import Path

PEN_MARKER = r'\[PEN:A3,ACTIVE=(HIGH|LOW),ON_TICKS=(\d+),OFF_TICKS=(\d+),TICK_MS_NOMINAL=(\d+)\]'
GUARD_ON_TICKS = 60
GUARD_OFF_TICKS = 61
GUARD_TICK_MS_NOMINAL = 32
DEFAULT_UP_MS = 250
DEFAULT_DOWN_MS = 250
COOLDOWN_MS = 2200
DRAW_SPAN_S = .45
MAX_BATCH_POINTS = 64


def firmware_profile(active_low, continuous=False):
    return {'active_low':active_low,'on_ticks':0 if continuous else GUARD_ON_TICKS,
            'off_ticks':0 if continuous else GUARD_OFF_TICKS,'tick_ms_nominal':GUARD_TICK_MS_NOMINAL}


@dataclass(frozen=True)
class PenLiftSettings:
    enabled: bool = False
    active_low: bool | None = None
    confirmed: bool = False
    up_ms: int = DEFAULT_UP_MS
    down_ms: int = DEFAULT_DOWN_MS
    power_raises: bool = False
    continuous: bool = False

    def validate(self, *, require_ready=False):
        for value in (self.enabled, self.confirmed, self.power_raises, self.continuous):
            if type(value) is not bool:
                raise ValueError('Некорректный режим подъёма пера.')
        if self.active_low is not None and type(self.active_low) is not bool:
            raise ValueError('Полярность реле должна быть проверена: HIGH или LOW.')
        if any(type(n) is not int or not 50 <= n <= 500 for n in (self.up_ms,self.down_ms)):
            raise ValueError('Задержки подъёма и опускания: от 50 до 500 мс.')
        if require_ready and self.enabled and (self.active_low is None or not self.confirmed):
            raise ValueError('Сначала сохраните полярность реле и проверенное направление подъёма ручки в настройках.')
        return self

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_value(cls,value):
        if isinstance(value,cls):return value.validate()
        if value is None:return cls()
        if not isinstance(value,dict) or set(value)-set(cls.__dataclass_fields__):
            raise ValueError('Некорректный профиль ручки.')
        return cls(**value).validate()

    def for_demo(self):
        return PenLiftSettings(self.enabled,False,True,self.up_ms,self.down_ms,
                               self.power_raises,self.continuous).validate()

    def coil_for(self, down):
        """Coil power, not HIGH/LOW: the firmware owns electrical polarity."""
        if type(down) is not bool:
            raise ValueError('Положение ручки задаётся логическим значением.')
        return self.power_raises != down


def load_settings(directory):
    default=PenLiftSettings(enabled=True)
    if directory is None:return default
    path=Path(directory)/'pen-lift-settings.json'
    if not path.exists():return default
    try:return PenLiftSettings.from_value(json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError,json.JSONDecodeError):return default


def save_settings(directory,settings):
    settings.validate()
    if directory is None:return
    folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
    target=folder/'pen-lift-settings.json';temporary=folder/'pen-lift-settings.json.tmp'
    temporary.write_text(json.dumps(settings.as_dict(),indent=2),encoding='utf-8')
    temporary.replace(target)


def connection_notice(enabled=False, power_raises=False, continuous=False):
    if enabled:
        if continuous:
            return ('Оранжевый — переходы с поднятым пером, без дробления и пауз охлаждения. '
                    'Ограничение времени соленоида отключено. '
                    + ('После остановки питание выключено, ручка опущена.' if power_raises else
                       'После остановки питание выключено, ручка поднята.'))
        if power_raises:
            return ('Оранжевый — переходы с поднятым пером: питание поднимает ручку. '
                    'В паузах охлаждения она опускается на месте; возможны точки. '
                    'После остановки соленоид выключен, ручка опущена.')
        return 'Оранжевый — переходы с поднятым пером при проверенном режиме соленоида; это не линии рисунка.'
    return 'Оранжевый — переходы. В режиме без подъёма они ТОЖЕ рисуются.'


def batches(path,feed):
    """Split long strokes without changing geometry or lifting mid-move.

Each batch resumes at its exact last endpoint. The hardware guard remains the
final cutoff if actual acceleration/USB timing exceeds this conservative plan.
"""
    if not math.isfinite(feed) or feed<=0:raise ValueError('Некорректная подача.')
    budget=feed/60*DRAW_SPAN_S
    current=path[0];points=[];used=0.
    for target in path[1:]:
        while math.dist(current,target)>1e-9:
            distance=math.dist(current,target);available=max(0.,budget-used)
            if available<1e-9 or len(points)>=MAX_BATCH_POINTS:
                if points:yield points
                points=[];used=0.;available=budget
            amount=min(distance,available)
            if amount<=0:raise ValueError('Подача слишком мала для планирования подъёма.')
            point=target if amount>=distance-.000001 else tuple(current[i]+(target[i]-current[i])*amount/distance for i in (0,1))
            points.append(tuple(point));used+=amount;current=tuple(point)
    if points:yield points
