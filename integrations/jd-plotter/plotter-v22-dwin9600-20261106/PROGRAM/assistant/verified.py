"""Pure consultation policy shared by both assistant windows.

Answers from this module cannot carry a plan, controller or executable tool.
Unmatched project/electrical/calculation questions fail closed, rather than
letting a language model fill in missing parameters. General topics may use it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Mapping

from .calculations import calculate, is_calculation_request
from .path_metrics import time_lower_bound
from .planner import ACTION_KINDS, parse
from .project_answers import lookup, is_project_topic, sheet_dimensions, urgent_answer

VERSION = "plotter-verified-v1"
VIRTUAL_EDITOR = {"sheet_width": 3000, "sheet_height": 3000, "has_paths": True}


@dataclass(frozen=True)
class VerifiedReply:
    text: str
    topic: str
    source: str = "Проверенная справка проекта"
    sources: tuple[str, ...] = ("assistant/knowledge.md",)
    urgent: bool = False


def number(state: Mapping, key: str, *, positive: bool = False) -> float | None:
    item = state.get(key)
    if (not isinstance(item, (int, float)) or isinstance(item, bool) or
            item < 0 or item > 1_000_000_000 or not math.isfinite(item) or
            (positive and item == 0)):
        return None
    return float(item)


def editor_report(state: Mapping) -> str:
    """Only bounded snapshot fields; not a sensor reading or permission to draw."""
    dimensions = sheet_dimensions(state)
    lines = ["Это проверка данных редактора, не измерение станка и не запуск."]
    if state.get("sandbox"):
        lines.append("Выбран пробный макет; его область не является калибровкой плоттера.")
    if dimensions:
        lines.append(f"Область редактора: {dimensions[0]:g} × {dimensions[1]:g} мм.")
    else:
        lines.append("Область не задана: для реального рисунка нужны оба угла в пульте.")
    if not state.get("has_paths"):
        lines.append("Эскиз не загружен; оценить маршрут пока нельзя.")
        return "\n".join(lines)
    for key, label in (("path_count", "Контуров"), ("point_count", "Точек"),
                       ("connection_count", "Переходов между контурами")):
        count = number(state, key)
        if count is not None and count.is_integer():
            lines.append(f"{label}: {count:g}.")
    width, height = number(state, "drawing_width"), number(state, "drawing_height")
    if width is not None and height is not None:
        lines.append(f"Габариты эскиза: {width:g} × {height:g} мм. Габариты сами по себе "
                     "не подтверждают его расположение внутри листа.")
    length = number(state, "travel_length_mm")
    connections = number(state, "connection_length_mm")
    if length is not None:
        lines.append(f"Маршрут с соединениями: {length:g} мм; начальный подвод от головки не включён.")
    if connections is not None:
        lines.append(f"Соединения контуров: {connections:g} мм. Без подъёма ручки они тоже рисуются.")
    feed = number(state, "feed_mm_min", positive=True)
    if length is not None and feed is not None:
        seconds = time_lower_bound(length, feed)
        lines.append(f"При подаче {feed:g} мм/мин идеальная нижняя оценка: {seconds:g} с. "
                     "Разгон, торможение, повороты, USB, начальный подвод, подъём пера и паузы охлаждения не учтены.")
    else:
        lines.append("Для расчётного времени нужны длина маршрута и положительная подача.")
    if state.get("busy"):
        lines.append("Редактор сообщает о текущей операции: изменения и новый запуск недоступны.")
    lines.append("Перед первым прогоном проверьте границы и соединения на экране; "
                 "физический запуск — отдельно в пульте, сначала без ручки. "
                 "Пропуски шагов и ручной сдвиг этот отчёт не обнаруживает.")
    return "\n".join(lines)


def _hardware_question(text: str) -> bool:
    # English aliases and context-poor wiring questions also stay local.
    return bool(re.search(
        r"\b(?:stepper|wiring|pinout|voltage|homing|plotter|motor|dwin|vref|amperage|amps?|volts?|ohms?)\b|"
        r"\b(?:what|how|which|set|adjust)\b.{0,35}\b(?:current|resistance)\b|"
        r"\bconnect\w*\b.{0,60}\b(?:wire|board|pin|connector|plug)\b|"
        r"\b(?:wire|board|pin|connector|plug)\b.{0,60}\bconnect\w*\b|"
        r"(?:куда|как|можно).{0,45}(?:подключ|переподключ|подавать|подать).{0,45}"
        r"(?:провод|красн|син|черн|зелен|контакт|штекер)|"
        r"(?:куда|как).{0,25}(?:красный|синий|черный|зеленый)\b|"
        r"(?:како[йе]|сколько|выстав|установ|настро).{0,35}(?:\bток\b|сопротивлен)|"
        r"\b(?:напряжени[ея]|вольт[а-я]*|ампер[а-я]*|распиновк[а-я]*)\b", text))


def consult(prompt: str, state: Mapping | None = None) -> VerifiedReply | None:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1800:
        return VerifiedReply("Введите один вопрос или расчёт до 1800 знаков.", "invalid_request")
    state = state if isinstance(state, Mapping) else {}
    emergency = urgent_answer(prompt)
    if emergency:
        return VerifiedReply(emergency.text, emergency.topic, sources=emergency.sources, urgent=True)
    from .task_memory import editor_question
    task_answer = editor_question(prompt, state)
    if task_answer is not None:
        return VerifiedReply(task_answer, 'task_context', source='Память задачи · данные редактора',
                             sources=('assistant/task_memory.py', 'assistant/chat_context.py'))
    # Recognize syntax only. The virtual state is never saved, applied or sent
    # to a controller; the real editor still checks prerequisites and bounds.
    proposed = parse(prompt, VIRTUAL_EDITOR)
    if proposed.kind in ACTION_KINDS:
        return VerifiedReply(
            "Это команда подготовки эскиза. В основном окне «Помощник» она требует "
            "проверки предложения и подтверждения «Применить к эскизу». "
            "Ответ в чате не применяет эскиз и не двигает моторы.",
            "preview_command", source="Проверенная программа", sources=("assistant/planner.py",))
    text = re.sub(r"\s+", " ", prompt.casefold().replace("ё", "е")).strip()
    computed = calculate(prompt)
    if computed is not None:
        return VerifiedReply(computed, "calculation", source="Проверенный расчёт",
                             sources=("assistant/calculations.py",))
    if re.fullmatch(r"(?:какое\s+время\s+сейчас|который\s+час|сколько\s+сейчас\s+времени|"
                    r"какая\s+(?:сегодня\s+)?дата|какое\s+сегодня\s+число|какой\s+сегодня\s+день)[.!?]?", text):
        current = datetime.now().astimezone()
        return VerifiedReply("По часам этого компьютера: " + current.strftime("%d.%m.%Y %H:%M:%S (UTC%z)") +
                             ". Это системные часы, без проверки синхронизации через интернет.",
                             "local_clock", source="Системные часы компьютера", sources=())
    fact = lookup(prompt, state)
    # Count/report questions must use the actual bounded editor metrics, not
    # the older general reference about sheet dimensions and head position.
    if (fact is None or fact.topic == "editor_state") and (
            re.search(r"(?:проверь|оцени|анализ|отчет|отчёт).{0,30}(?:эскиз|рисунок|траектори|маршрут)", text) or
            re.search(r"сколько.{0,20}(?:контур|точек|соединени)", text)):
        return VerifiedReply(editor_report(state), "drawing_report", source="Данные редактора · расчёт",
                             sources=("assistant/preview.py", "assistant/path_metrics.py"))
    explicit_calculation = bool(re.search(r"\b(?:посчита\w*|рассчита\w*|вычисли\w*|переведи\w*|пересчита\w*)\b", text))
    # In a grounded speed question, 100% names the top of the UI scale.
    numeric_text = re.sub(r'\b100\s*%', '', text) if fact and fact.topic == 'speed' else text
    has_numeric_input = bool(re.search(r"\d|\b(?:nan|inf)\b", numeric_text))
    # Conceptual source-backed questions (including estimate from editor data)
    # do not require the user to invent numbers. Unsupported numerical requests
    # still cannot borrow a generic reference's unrelated coefficient.
    if fact and not has_numeric_input and not explicit_calculation:
        return VerifiedReply(fact.text, fact.topic, sources=fact.sources)
    device_calculation = bool(re.search(r"импульс|коэффициент|подач|маршрут|траектор|"
                                       r"\bпуть\b|\bход\b|\bмм\b|вписать|пол[еях]", text))
    if is_calculation_request(prompt) and (has_numeric_input or explicit_calculation or
                                          device_calculation or is_project_topic(prompt)):
        return VerifiedReply(
            "Не могу надёжно разобрать этот расчёт. Укажите одну операцию, все исходные "
            "числа и нужные единицы; отрицательные размеры, диапазоны и несколько "
            "операций не вычисляются частично. Поддерживаются поля листа, ход по импульсам, "
            "проценты, перевод длины/массы/времени, подача, частота импульсов, время, вписывание и условная коррекция "
            "масштаба. Для неподдержанного расчёта укажите недостающие данные. "
            "Команды и настройки не изменены.", "calculation_needs_data",
            source="Нужны данные · без догадок", sources=("assistant/calculations.py",))
    if re.search(r"[$`]|```|\b(?:g\s*\d+|m\s*\d+)\b|"
                 r"(?:игнорируй|ignore\b|exec\s*\(|eval\s*\(|cmd\.exe|powershell)", text):
        return VerifiedReply("Код, G-code и обход правил не выполняются. Изменений нет. "
                             "Пишите одну команду эскиза или вопрос без исполняемого кода.",
                             "non_executing", source="Проверенная программа")
    if re.fullmatch(r"(?:что сейчас загружено|что загружено|состояние эскиза|статус эскиза)[.!?]?", text):
        return VerifiedReply(editor_report(state), "editor_state", source="Данные редактора · расчёт")
    if fact:
        return VerifiedReply(fact.text, fact.topic, sources=fact.sources)
    if is_project_topic(prompt) or _hardware_question(text):
        return VerifiedReply(
            "По этому вопросу о сборке пока недостаточно проверенных данных. "
            "Уточните компонент и пришлите маркировку, "
            "измерение или текст ошибки. Цвет провода не подтверждает контакт; "
            "переподключения возможны только после отключения всех источников, включая USB. "
            "Команды и настройки станка не изменялись.", "project_needs_data",
            source="Нужны данные · без догадок")
    return None


def _dependent_question(prompt: str) -> bool:
    text = prompt.casefold().replace("ё", "е").strip()
    return (bool(re.search(r"^(?:а\s+)?(?:сколько\s+туда|(?:красный|синий|черный|зеленый)\s+куда|"
                               r"куда\s+его|как\s+его|а\s+если|а\s+при)\b", text)) or
                 bool(re.search(r"^(?:(?:а|and|what\s+about)\s+)?(?:the\s+)?"
                                r"(?:red|blue|black|green|красн\w*|син\w*|черн\w*|зелен\w*)\b", text)) or
                 bool(re.search(r"^(?:а|and)\s+(?:можно\b|\d)", text)) or
                 bool(re.fullmatch(r"(?:а\s+)?(?:почему|как|сколько|куда|это\s+точно|можно\s+так|"
                                   r"так\s+можно|так\s+правильно|это\s+безопасно|это\s+правильно|"
                                   r"что\s+дальше|что\s+теперь|давай\s+так|is\s+it\s+safe|"
                                   r"is\s+that\s+right|what\s+next)[.!?]?", text)) or
                 bool(re.search(r"^(?:а\s+)?(?:какое|какая|какой|сколько).{0,20}\b(?:туда|там|ему|ей|его|ее)\b", text)))


def context_followup(prompt: str, history) -> VerifiedReply | None:
    """No inherited coefficients or guessed pins from short conversational turns."""
    if not isinstance(prompt, str) or not isinstance(history, (list, tuple)) or not history:
        return None
    if not _dependent_question(prompt):
        return None
    protected_context = False
    for previous in reversed(history[-4:]):
        if not isinstance(previous, (tuple, list)) or len(previous) != 2 or not isinstance(previous[0], str):
            break
        if (is_project_topic(previous[0]) or _hardware_question(previous[0].casefold()) or
                is_calculation_request(previous[0])):
            protected_context = True
            break
        if not _dependent_question(previous[0]):
            break  # An explicit unrelated question starts a new subject.
    if not protected_context:
        return None
    return VerifiedReply(
        "Уточните, о каком компоненте или расчёте речь, и повторите исходные данные. "
        "Я не переношу контакты, напряжения или коэффициенты из предположений прошлой "
        "реплики. Нужны маркировка/документация для подключения или числа с единицами "
        "для расчёта. Изменений нет.", "context_needs_data", source="Нужны данные · без догадок")
