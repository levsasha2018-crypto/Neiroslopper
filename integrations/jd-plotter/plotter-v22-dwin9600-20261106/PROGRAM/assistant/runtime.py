"""Local assistant orchestration. Never imports a hardware controller or executes code."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
import threading
import time
from typing import Callable, Mapping

from . import codex_assistant, cloud_backend
from .calculations import calculate
from .planner import ACTION_KINDS, Plan, parse
from .project_answers import (lookup as lookup_project_answer, is_project_topic,
                              sheet_dimensions, is_display_topic, mentions_forty_steps,
                              editor_words_only, pen_motion_description, display_description,
                              profile_hardware_answer)
from .verified import consult, VIRTUAL_EDITOR
from .tool_router import route as route_program, preview_request

EXAMPLES = (
    "По центру",
    "Поверни рисунок на 90 градусов",
    "Ширина рисунка 80 мм",
    "Уменьши рисунок до 50%",
    "Впиши рисунок с полями 5 мм",
    "Отрази рисунок по горизонтали",
    "Сдвинь рисунок вправо на 5 мм",
    "Напиши привет высотой 2 мм",
    "Напиши печатными «Привет Hello» высотой 5 мм",
    "Добавь рамку с отступом 3 мм",
    "Создай линейку длиной 50 мм",
    "Создай тест точности размером 20 мм",
    "Напиши привет в левом верхнем углу листа",
    "Сколько времени займёт рисование?",
    "Что сейчас загружено?",
    "Сделай надпись в 5 раз больше",
    "Нарисуй маленькую гору и солнце над ней",
    "Напиши «Привет» по центру и добавь рамку с отступом 3 мм",
)
@dataclass
class Reply:
    text: str
    plan: Plan | None = None
    source: str = "Проверенная программа"
    elapsed_s: float = 0.0
    context_request: str = ""
    waiting_field: str = ""
    corrects_previous: bool = False
    waiting_edit: dict = field(default_factory=dict)
    selection_id: str | None = None
    select_all: bool = False
    variants: list = field(default_factory=list)
    studio_base: dict | None = None
    template_name: str = ''
    llm_metrics: list = field(default_factory=list)
    validation_error: str = ''


def preview_cancel(prompt: str) -> str | None:
    """Closed grammar for cancelling a proposal, never motion or applied edits.

    Quoted text and longer/compound requests intentionally do not match.
    """
    if not isinstance(prompt, str):
        return None
    text = re.sub(r'\s+', ' ', prompt.casefold().replace('ё', 'е')).strip()
    if not re.fullmatch(r'(?:пожалуйста[, ]+)?(?:отмена|отмени|не надо|передумал|передумала|'
                        r'отмен(?:и|ить) (?:это )?(?:предложение|черновик|запрос)|'
                        r'убери (?:это )?предложение)(?:[, ]+пожалуйста)?[.!]?', text):
        return None
    return ('Отменён только запрос и неприменённое предложение. Подтверждённый эскиз сохранён. '
            'Это не СТОП станка и не отмена уже применённой правки.')


def describe_state(state: Mapping) -> str:
    """Only report values supplied by the editor, not inferred physical measurements."""
    lines = []
    if state.get("sandbox"):
        lines.append("Пробный макет: USB не подключён этим помощником. Это не калибровка станка.")
    elif state.get("connected"):
        lines.append("Редактор подключён к DEMO." if state.get("simulation") else
                     "Редактор сообщает о подключении к контроллеру; я не управляю его соединением.")
    else:
        lines.append("Редактор не подключён к контроллеру.")
    dimensions = sheet_dimensions(state)
    if dimensions:
        width, height = dimensions
        lines.append(f"Область в редакторе: {width:g} × {height:g} мм.")
    else:
        lines.append("Область в редакторе ещё не задана. На станке сохраните два угла вручную.")
    if state.get("has_paths"):
        lines.append(f"Эскиз: {state.get('file_name', 'рисунок')}; "
                     f"контуров: {state.get('path_count', '?')}, точек: {state.get('point_count', '?')}.")
        if state.get("drawing_width") is not None:
            lines.append(f"Габариты траектории: {state['drawing_width']:.2f} × "
                         f"{state['drawing_height']:.2f} мм.")
    else:
        lines.append("Эскиз не выбран.")
    from pen_lift import connection_notice
    lines.append(connection_notice(state.get("pen_lift_enabled") is True,
                                   power_raises=state.get("pen_power_raises") is True,
                                   continuous=state.get("pen_continuous") is True))
    if state.get("pen_lift_enabled") and not state.get("pen_lift_ready"):
        lines.append("Автоподъём выбран, но профиль реле ещё не подтверждён; запуск заблокирован до сохранения настроек в пульте.")
    lines.append("Концевиков нет. Есть отдельное окно «Камера / сканер»; "
                 "этот снимок редактора не подтверждает подключение или измерения камеры.")
    return "\n".join(lines)


def factual_answer(prompt: str, state: Mapping | None = None) -> str | None:
    """A few audited project facts stay deterministic instead of being re-invented."""
    text = prompt.casefold().replace("ё", "е")
    selected = profile_hardware_answer(prompt, state or {})
    if selected is not None:
        return selected.text
    computed = calculate(prompt)
    if computed:
        return computed
    if (any(word in text for word in ("функци", "возможност", "идеи", "умеет")) and
            any(word in text for word in ("помощник", "ассистент", "ии"))):
        return ("Уже готово: изменение эскиза явными командами — центр, масштаб, ширина, "
                "поворот, отражение, сдвиг и вписывание с полями; проверка границ и подтверждение. "
                "Есть печатные надписи на русском и латинице, рамка вокруг рисунка, "
                "линейка с делениями 1 мм и тест точности. Рукописным остаётся только "
                "готовый образец «привет». SVG уже открывается в основном пульте; "
                "текущий эскиз можно сохранить в SVG из помощника. "
                "Если готовая команда не подходит, свободный план Codex позволяет "
                "предложить композицию, до восьми операций и собственные линии; смысл нужно проверить.\n\n"
                "Добавлены проекты с параметрами надписей, многошаговая отмена/повтор и "
                "восстановление текста при повторном открытии. Отдельное окно камеры "
                "поддерживает видео, калибровку, проходы и сканы PNG/PDF; оборудование "
                "ещё нужно настроить и проверить.\n\n"
                "В инструментах редактора есть выбор отдельных объектов, автосохранение, история, "
                "текстовые блоки с переносами, три оформления, оптимизация контуров, свои шаблоны "
                "и мастер расчёта точности по вашим измерениям. Дополнительные гарнитуры и "
                "произвольный рукописный текст пока не реализованы.\n\n"
                "В пульте добавлено управление соленоидом с профилем выбранной платы. " + pen_motion_description(state) +
                "Нужны подходящая прошивка и правильные настройки направления и HIGH/LOW. "
                "Его положения и перегрузки моторов "
                "мы сейчас не измеряем. Помощник не запускает движение.")
    if is_display_topic(prompt):
        return display_description(state)
    if mentions_forty_steps(prompt) or "шагов/мм" in text:
        return ("40 шагов/мм — коэффициент расстояния, не скорость. Для двигателя 1,8°, "
                "ремня GT2, шкива 20 зубьев и микрошагов 1/8: 200 × 8 / (2 × 20) = 40 шагов/мм. "
                "Реальные перемычки и пройденное расстояние нужно проверить на сборке.")
    if any(w in text for w in ("максимальн", "максимум", "100%", "100 %")) and "скорост" in text:
        return ("100% в нашей программе — заданный профиль 3000 мм/мин, не измеренный максимум моторов. "
                "На пропуски шагов влияют нагрузка, ускорение, ток, питание и механика. "
                "Помощник не меняет скорость или настройки контроллера.")
    if any(w in text for w in ("шип", "шум", "гуд")) and any(w in text for w in ("просто", "удерж", "мотор")):
        return ("Шипение в простое может быть связано с работой A4988 при удержании; "
                "по звуку одному нельзя исключить перегрев или неверную настройку. "
                "Галочка «Удерживать моторы в простое» есть в основном пульте. "
                "Без удержания оси могут сместиться, и тогда оба угла нужно задать заново. "
                "Этот чат не переключает удержание.")
    if "vref" in text or any(w in text for w in ("ток драйвер", "ток a4988")):
        return ("Номинал двигателя 1,7 А не задаёт безопасную настройку A4988 автоматически. "
                "Для расчёта Vref нужен номинал измерительных резисторов именно вашего модуля; "
                "допустимый ток также зависит от платы и охлаждения. Нужны фото маркировки резисторов. "
                "Без этих данных конкретное Vref не назначаю.")
    if any(w in text for w in ("пенлифт", "подъем пера", "подъем ручк", "поднимать ручк", "поднимает ручк")):
        return ("В пульте есть режим соленоида для выбранной платы. " + pen_motion_description(state) +
                "Полярность HIGH/LOW определяет включение реле, а не направление движения ручки. "
                "Без автоподъёма соединения тоже рисуются. "
                "Помощник только консультирует, реле не переключает.")
    return None


class Assistant:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir

    @cloud_backend.scoped_answer
    def answer(self, prompt: str, state: Mapping, *, use_llm: bool = True,
               allow_creative: bool = True,
               cancel: threading.Event | None = None,
               on_chunk: Callable[[str], None] | None = None) -> Reply:
        from .draft_context import resolve, chain
        from .dialog_context import normalize_followup, target_reply, corrects_scale_request
        from .planner import transform_paths
        started = time.perf_counter()
        # Actual unpublished editor proposal, not guessed conversational memory.
        # Emergency reporting must not depend on rendering an older proposal.
        urgent = consult(prompt, state)
        if urgent is not None and urgent.urgent:
            return Reply(urgent.text, source=urgent.source)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1800:
            return Reply("Введите короткую команду или вопрос — до 1800 знаков.")
        if cancel is not None and cancel.is_set():
            return Reply("Запрос отменён.")
        cancelled_preview = preview_cancel(prompt)
        if cancelled_preview is not None:
            return Reply(cancelled_preview, source='Отмена предложения · без движения')
        if (use_llm and (state.get('panel_reply') or codex_assistant.is_question(prompt)) and
                not state.get('web_drawing') and not preview_request(prompt) and
                (urgent is None or urgent.topic != 'preview_command')):
            return self._ask_codex(prompt, state, urgent, cancel, on_chunk,
                                   allow_preview=allow_creative and state.get('studio_selected') is None)
        from .task_memory import describe
        remembered = describe(prompt, state.get('task_memory'))
        if remembered is not None:
            return Reply(remembered, source='Память задачи · данные редактора')
        from .studio_commands import respond as studio_respond
        studio_reply = studio_respond(prompt,state,cancel)
        if studio_reply is not None:
            studio_reply.elapsed_s = time.perf_counter()-started
            return studio_reply
        if state.get('studio_selected') is not None and state.get('studio_scene') is not None:
            # Give Codex freedom inside the selected object, never in its neighbours.
            from copy import deepcopy
            from .scene import checked_scene, scene_plan, from_plan, render_scene
            try:
                scene, groups = checked_scene(state['studio_scene'],state['sheet_width'],state['sheet_height'])
                index = next(i for i,o in enumerate(scene['objects']) if o['id']==state['studio_selected'])
                scoped = {k:v for k,v in state.items() if not k.startswith('studio_')}
                scoped.update(editor_paths=groups[index],preview_task=None,preview_draft=None,preview_anchor=None,retry_draft=None)
                reply = self.answer(prompt,scoped,use_llm=use_llm,allow_creative=allow_creative,cancel=cancel,on_chunk=on_chunk)
                if reply.plan is not None:
                    rendered = transform_paths(groups[index],reply.plan,state['sheet_width'],state['sheet_height'])
                    rebuilt = from_plan(reply.plan,rendered,state['sheet_width'],state['sheet_height'])
                    obj = scene['objects'][index]
                    if len(rebuilt['objects'])==1:
                        obj['source'],obj['matrix'] = deepcopy(rebuilt['objects'][0]['source']),rebuilt['objects'][0]['matrix']
                    else:
                        obj['source']={'kind':'paths','parameters':{'paths':rendered}}
                        obj['matrix']=[1,0,0,1,0,0]
                    scene['route']=None
                    render_scene(scene,state['sheet_width'],state['sheet_height'])
                    reply = replace(reply,plan=scene_plan(scene,reply.plan.explanation),
                        text=reply.text+'\nИзменяется только выбранный объект. Если созданы произвольные линии, они не считаются редактируемыми буквами.')
                reply.elapsed_s=time.perf_counter()-started
                return reply
            except (ValueError,TypeError,KeyError,OverflowError,StopIteration):
                return Reply('Предложение для выбранного объекта некорректно или не помещается. Остальная композиция сохранена.')
        effective = normalize_followup(prompt, state)
        correction = corrects_scale_request(prompt, state)
        if correction and isinstance(state.get("retry_draft"), Plan):
            state = {**state, "preview_draft": state["retry_draft"]}
        # Text-property edits rebuild a known generator, not a new scale on
        # top of the old draft geometry. Assistant prose is never parsed here.
        from .text_editing import edit
        edited = edit(effective, state)
        if edited is None:
            contextual = target_reply(effective, state)
            if contextual is not None:
                return Reply(contextual, source="Контекст редактора · без догадок",
                             context_request=state.get("last_preview_request", ""),
                             waiting_field=state.get("awaiting_text_field", ""),
                             waiting_edit={key: state[key] for key in ("awaiting_text_request", "awaiting_text_target") if key in state})
        if edited is not None:
            if not edited.requires_confirmation:
                return Reply(edited.question or edited.explanation,
                             source="Редактирование текста · нужно уточнение",
                             context_request=state.get("last_preview_request", "") if edited.parameters.get("context_query") else effective,
                             waiting_field=state.get("awaiting_text_field", "") if edited.parameters.get("context_query") else
                                           edited.parameters.get("awaiting_text_field", ""),
                             waiting_edit={key: source[key] for source in [state if edited.parameters.get("context_query") else edited.parameters]
                                           for key in ("awaiting_text_request", "awaiting_text_target") if key in source})
            reply = Reply(edited.explanation + "\n\nПроверьте предложение и подтвердите «Применить к эскизу». "
                          "Станок не запускается.", plan=edited, source="Редактирование текста → проверенный расчёт",
                          context_request=effective)
            prefix = []
            working = state
        else:
            reply = None
        try:
            if reply is None:
                working, prefix = resolve(effective, state)
        except (ValueError, TypeError, KeyError, OverflowError):
            return Reply("Не удалось использовать предыдущее предложение. Примените или отмените его и повторите просьбу.")
        if reply is None:
            reply = self._answer_core(effective, working, use_llm=use_llm,
                                      allow_creative=allow_creative, cancel=cancel, on_chunk=on_chunk)
            if reply.plan is not None:
                reply.context_request = effective
        if cancel is not None and cancel.is_set():
            return Reply("Запрос отменён.")
        if reply.plan is not None and (prefix or "editor_paths" in state):
            try:
                if reply.plan.kind == 'object_scene':
                    prefix = []  # The new scene already describes the complete editor scope.
                combined = chain(prefix, reply.plan)
                transform_paths(state.get("editor_paths", ()), combined,
                                state.get("sheet_width"), state.get("sheet_height"))
                if prefix:
                    reply = replace(reply, plan=combined, text=combined.explanation +
                        "\n\nПроверьте всё предложение и подтвердите «Применить к эскизу». Станок не запускается.")
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                reply = replace(reply, plan=None, text="Это изменение не помещается в лист или некорректно. "
                    "Последний корректный вариант сохранён для уточнения; ничего не применено. "
                    "Можно задать меньший размер или коэффициент.", validation_error=str(exc)[:300])
        reply.elapsed_s = time.perf_counter() - started
        reply.corrects_previous = correction
        if (reply.plan is None and not reply.context_request and
                ("?" in prompt or re.match(r"\s*(?:почему|как|зачем|что|какой|какая|какие|объясни|сколько)\b",
                                           prompt.casefold()))):
            # A read-only question does not cancel the last editor operation
            # or a program's concrete pending field. It cannot create either.
            reply.context_request = state.get("last_preview_request", "")
            reply.waiting_field = state.get("awaiting_text_field", "")
            reply.waiting_edit = {key: state[key] for key in ("awaiting_text_request", "awaiting_text_target") if key in state}
        return reply

    def _ask_codex(self, prompt, state, checked, cancel, on_chunk, *, allow_preview=False):
        from .chat_context import bounded_history, chat_snapshot
        from .saved_chat import load

        started = time.perf_counter()
        try:
            answer = codex_assistant.consult_chat(prompt, on_chunk or (lambda _: None), cancel,
                state=chat_snapshot(state), history=bounded_history(state.get('chat_history', ())),
                resume_thread=load(self.data_dir), verified=checked, editor_state=state if allow_preview else None)
            if answer.get('cancelled'):
                return Reply('Запрос отменён.')
            return Reply(answer['answer'], plan=answer.get('plan'), source=answer['source'],
                         elapsed_s=time.perf_counter() - started,
                         context_request=prompt if answer.get('plan') is not None else state.get('last_preview_request', ''),
                         waiting_field=state.get('awaiting_text_field', ''),
                         waiting_edit={key: state[key] for key in ('awaiting_text_request', 'awaiting_text_target') if key in state})
        except (ValueError, OSError) as exc:
            if cancel is not None and cancel.is_set():
                return Reply('Запрос отменён.')
            detail = str(exc) if isinstance(exc, ValueError) else 'Не удалось прочитать привязку чата.'
            return Reply(detail, source='Codex · ответ не получен')

    def _answer_core(self, prompt: str, state: Mapping, *, use_llm: bool = True,
               allow_creative: bool = True,
               cancel: threading.Event | None = None,
               on_chunk: Callable[[str], None] | None = None) -> Reply:
        started = time.perf_counter()
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1800:
            return Reply("Введите одну короткую команду или вопрос — до 1800 знаков.")
        if cancel is not None and cancel.is_set():
            return Reply("Запрос отменён.")
        checked = consult(prompt, state)
        if checked is not None and checked.urgent:
            return Reply(checked.text, source=checked.source, elapsed_s=time.perf_counter() - started)
        if state.get('web_drawing'):
            creative = self._creative(prompt, state, use_llm, allow_creative, cancel)
            if creative is not None:
                return Reply(creative.text, plan=creative.plan, source=creative.source,
                             elapsed_s=time.perf_counter() - started,
                             validation_error=creative.validation_error)
        plan = parse(prompt, state)
        from .spatial_text import plan as spatial_plan
        positioned = spatial_plan(prompt, state)
        if positioned is not None:
            plan = positioned
            if not positioned.requires_confirmation:
                return Reply(positioned.question or positioned.explanation, source="Размещение текста · нужно уточнение")
        # Keep exact commands, their prerequisites and program-built
        # compositions ahead of the model.
        known_syntax = parse(prompt, VIRTUAL_EDITOR).kind in ACTION_KINDS
        if plan.kind not in ACTION_KINDS | {"help", "status"} and "editor_paths" in state:
            from .creative_planner import compose_known
            composed = compose_known(prompt, state)
            if composed is not None:
                return Reply(composed.text, plan=composed.plan, source=composed.source,
                             elapsed_s=time.perf_counter() - started)
        # The exact-command parser refuses the word «нарисуй» as a motor
        # command. The creative channel has its own preview-only allowlist.
        creative_topics = {"handwriting", "svg", "svg_margin"}
        if (plan.kind not in ACTION_KINDS | {"help", "status"} and not known_syntax and
                (checked is None or checked.topic in creative_topics or
                 checked.topic == 'project_needs_data' and editor_words_only(prompt))):
            creative = self._creative(prompt, state, use_llm, allow_creative, cancel)
            if creative is not None:
                return Reply(creative.text, plan=creative.plan, source=creative.source,
                             elapsed_s=time.perf_counter() - started,
                             validation_error=creative.validation_error)
        if (plan.kind not in ACTION_KINDS | {"help", "status", "refused"} and not known_syntax and
                (checked is None or checked.topic in {"project_needs_data", "calculation_needs_data"} or preview_request(prompt))):
            routed = route_program(prompt, state, use_llm=use_llm, cancel=cancel)
            if routed is not None:
                if routed.plan is None and routed.program == "clarify" and not (routed.cancelled or routed.backend_failed):
                    creative = self._creative(prompt, state, use_llm, allow_creative, cancel)
                    if creative is not None:
                        return Reply(creative.text, plan=creative.plan, source=creative.source,
                                     elapsed_s=time.perf_counter() - started,
                                     validation_error=creative.validation_error)
                    if use_llm and not routed.validation_error:
                        return self._ask_codex(prompt, state, checked, cancel, on_chunk, allow_preview=allow_creative)
                return Reply(routed.text, plan=routed.plan, source=routed.source,
                             elapsed_s=time.perf_counter() - started,
                             validation_error=routed.validation_error)
        if use_llm and plan.kind not in ACTION_KINDS and not known_syntax:
            return self._ask_codex(prompt, state, checked, cancel, on_chunk, allow_preview=allow_creative)
        calculation = calculate(prompt)
        grounded = lookup_project_answer(prompt, state)
        facts = checked.text if checked is not None else (
            calculation or (grounded.text if grounded else factual_answer(prompt, state)))
        facts_source = checked.source if checked is not None else "Проверенная справка проекта"
        if plan.kind in ACTION_KINDS:
            result = Reply(plan.explanation + "\n\nЭто изменение эскиза, а не движение станка. "
                           "Сначала проверьте предложение, затем нажмите «Применить к эскизу».",
                           plan=plan, source="Команда → проверенный расчёт")
        elif plan.kind == "status":
            result = Reply(describe_state(state))
        elif plan.kind == "help":
            result = Reply("Умею готовить эскиз; каждая операция требует подтверждения. Примеры:\n\n" +
                           "\n".join(EXAMPLES) + "\n\nРукописный текст — только готовое однолинейное "
                           "слово «привет»: 1,5 / 2 / 3 мм по основной высоте букв, петли выше. "
                           "Печатный текст поддерживает русский и латинский алфавиты; "
                           "текст заключайте в кавычки и явно задавайте высоту. "
                           "Со свободным планом Codex "
                           "нестандартный рисунок можно описать своими словами: модель предлагает "
                           "композицию и собственные линии, которые нужно проверить. "
                           "Запуск рисования, стрелки и настройки моторов — только в основном пульте.")
        elif plan.kind == "question" and parse(prompt, VIRTUAL_EDITOR).kind in ACTION_KINDS:
            result = Reply(plan.question or plan.explanation)
        elif facts and plan.kind not in ACTION_KINDS:
            result = Reply(facts, source=facts_source)
        elif plan.kind in {"refused", "question"}:
            result = Reply(plan.question or plan.explanation)
        else:
            if facts:
                result = Reply(facts, source="Проверенная справка проекта")
            elif is_project_topic(prompt):
                result = Reply("По этому вопросу о сборке пока недостаточно проверенных данных. "
                               "Не буду заменять их догадкой модели. Уточните, какой компонент или "
                               "эффект вы имеете в виду, и пришлите маркировку, измерение или текст "
                               "ошибки. Команды и настройки станка не изменялись.",
                               source="Нужны данные · без догадок")
            else:
                result = Reply(plan.question or "Не распознал безопасную команду. Напишите одну операцию "
                               "с явными единицами: например, «Ширина рисунка 80 мм». Для свободных "
                               "вопросов используйте Codex.")
        result.elapsed_s = time.perf_counter() - started
        return result

    @staticmethod
    def _creative(prompt, state, use_llm, allow_creative, cancel):
        # Only an editor that supplies actual immutable geometry can receive
        # model-authored operations. Read-only chat has no such authority.
        if "editor_paths" not in state:
            return None
        from .creative_planner import route
        return route(prompt, state, use_llm=use_llm, allow_creative=allow_creative, cancel=cancel,
                     reference=state.get('web_drawing'))
