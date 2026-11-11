"""Small, editor-grounded follow-ups; displayed chat is never an authority.

The GUI supplies fresh ``preview_draft`` / ``preview_task`` Plan metadata and
``last_preview_request`` from the human. This module never stores conversation,
executes an edit, contacts a model or touches a controller. A normalized request
must still pass the normal parser, renderer and confirmation workflow.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Mapping

from .planner import Plan, _VALUE, _request_text, parse, transform_paths
from .text_editing import (_MM, _ORDINAL, _correct_command_typos, _edit_request_text,
                           _operations as _caption_operations, _replaces_source)


_FACTOR = r"в\s*([0-9а-я .,\-+]{1,80}?)\s*раз(?:а)?"
_RETRY = re.compile(
    r"(?:(?:тогда|ну\s+тогда|теперь|лучше|ладно|а|"
    r"давай(?:\s+тогда)?|нет,?\s*тогда)\s*)?"
    + _FACTOR, re.IGNORECASE)
_MORE_SCALE = re.compile(r"(?:(?:а|и|давай|теперь)\s+)?еще\s+" + _FACTOR, re.IGNORECASE)
_REFERENCE = re.compile(
    r"(?:размер\s+(?:надписи|текста|слова)|"
    r"(?:этой|той)\s+(?:надписи|текста)|"
    r"(?:тот|та|то|текст|надпись)\s+(?:про\s+)?"
    r"котор(?:ый|ую|ое)\s+(?:я\s+)?"
    r"(?:говорил|говорила|писал|писала|просил|просила)\s+(?:ранее|раньше))")
_CAPTION = r"(?:надпись|надписи|текст|текста|слово|слова)"
_DIRECTION = r"(?:вправо|влево|вверх|вниз|правее|левее|выше|ниже)"
_POSITION_FRAGMENT = re.compile(
    r"(?:(?:а\s+)?(?:еще|теперь|давай)(?:\s+еще)?\s+)?"
    r"(?P<direction>" + _DIRECTION + r")"
    r"(?:\s+(?:на\s*)?(?P<distance>" + _VALUE + r")\s*" + _MM + r")?")
_MISSING_EDIT = re.compile(
    r"(?:(?:сдвинь|перемести|перенеси)\s+" + _CAPTION + r"\s+(" + _DIRECTION + r")|"
    r"(поверни)\s+" + _CAPTION + r"|"
    r"(увеличь|уменьши)\s+" + _CAPTION + r"|"
    r"сделай\s+" + _CAPTION + r"\s+(больше|меньше)|"
    r"(размести|помести)\s+" + _CAPTION + r")")
_QUOTE = re.compile(r'«([^«»]*)»|"([^"\n]*)"|“([^“”]*)”')
_ORDINALS = {"перв": 0, "втор": 1, "трет": 2, "четверт": 3,
             "пят": 4, "шест": 5, "седьм": 6, "восьм": 7}
_BARE_CHOICE = re.compile(
    r"(?:" + _ORDINAL +
    r"(?:\s+" + _CAPTION + r")?|" + _CAPTION + r"\s+номер\s+[1-8]|[1-8])")


@dataclass(frozen=True)
class _Target:
    text: str | None
    draft: bool
    captions: tuple[str, ...] = ()
    other_geometry: bool = False


def _short(prompt):
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 1800:
        return None
    text = re.sub(r"\s+", " ", prompt.casefold().replace("ё", "е")).strip()
    # A numbered answer, as in the screenshot, is not a new drawing command.
    text = re.sub(r"^\d{1,2}[.)]\s*", "", text)
    return text.strip(" .!?")


def _numbers_ready(state):
    for key in ("sheet_width", "sheet_height"):
        value = state.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 3000:
            return False
        if not math.isfinite(value):
            return False
    return True


def _plan_operations(plan):
    if not isinstance(plan, Plan) or not isinstance(plan.parameters, dict):
        raise ValueError("Нет проверенного плана.")
    if plan.kind == "free_preview":
        if set(plan.parameters) != {"operations"}:
            raise ValueError("Некорректный составной план.")
        operations = plan.parameters["operations"]
        if not isinstance(operations, (tuple, list)) or not 1 <= len(operations) <= 8:
            raise ValueError("Некорректное число операций.")
    else:
        operations = [{"kind": plan.kind, "parameters": plan.parameters}]
    for item in operations:
        if (not isinstance(item, dict) or set(item) != {"kind", "parameters"} or
                not isinstance(item["kind"], str) or not isinstance(item["parameters"], dict)):
            raise ValueError("Некорректная операция.")
        # Shape/type validation without replaying an applied transformation.
        # Replaying could spuriously reject e.g. a valid already-scaled plan.
        from .free_preview import _validate_operation
        _validate_operation(item)
    return operations


def _caption_profile(plan, captions=(), other_geometry=False):
    """Track retained captions through replacing/appending typed generators."""
    if plan is None:
        return captions, other_geometry
    operations = _caption_operations(plan)
    if operations is None:
        raise ValueError("Некорректные сохранённые параметры надписи.")
    for item in operations:
        kind, parameters = item["kind"], item["parameters"]
        if kind in {"print_text", "text", "append_text"}:
            value = parameters.get("text")
            if not isinstance(value, str) or not 1 <= len(value) <= 800:
                raise ValueError("Некорректный текст.")
            if kind == "append_text":
                captions = (*captions, value)
            else:
                captions, other_geometry = (value,), False
        elif kind in {"ruler", "test_pattern"}:
            captions, other_geometry = (), True
        elif kind in {"polyline", "ellipse"}:
            if parameters.get("mode") == "replace":
                captions = ()
            other_geometry = True
        elif kind == "frame":
            other_geometry = True
    return captions, other_geometry


def _profile_target(plan, draft, captions=(), other_geometry=False):
    captions, other_geometry = _caption_profile(plan, captions, other_geometry)
    return _Target(captions[0] if len(captions) == 1 else None,
                   draft, captions, other_geometry)


def _target(state):
    if not isinstance(state, Mapping) or state.get("busy") or not _numbers_ready(state):
        return None
    width, height = state["sheet_width"], state["sheet_height"]
    paths = state.get("editor_paths", ())
    draft = state.get("preview_draft")
    task = state.get("preview_task")
    try:
        if draft is not None:
            draft_operations = _plan_operations(draft)
            # Render only the last valid proposal, never the previous failed
            # user scale request. The caller still renders/validates any edit.
            rendered = transform_paths(paths, draft, width, height)
            if not rendered:
                return None
            # A pending scale/move can be a delta over an applied task, not a
            # full reconstruction. Its retained captions still come from the
            # fresh applied Plan; a replacing generator resets that profile.
            captions, other = ((), bool(paths)) if _replaces_source(draft_operations) else _caption_profile(task, (), bool(paths))
            return _profile_target(draft, True, captions, other)
        if not state.get("has_paths"):
            return None
        # A boolean 'has_paths' alone is not proof of a usable drawing.
        transform_paths(paths, Plan("scale", {"percent": 100}), width, height)
        if task is not None:
            _plan_operations(task)
        return _profile_target(task, False, (), True)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def _previous_scale(state, target):
    previous = state.get("last_preview_request")
    if target is None or not isinstance(previous, str) or not 1 <= len(previous) <= 1000:
        return None
    known = dict(state)
    known["has_paths"] = True
    plan = parse(_correct_command_typos(_whole_alias(previous, known)), known)
    if plan.kind != "scale":
        return None
    value = plan.parameters.get("percent")
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value == 100):
        return None
    return plan


def _caption_alias(prompt, state, target):
    if (target is None or target.text is None or target.other_geometry or
            re.search(r"[«»“”\"']", prompt)):
        return prompt
    text = _short(prompt)
    if re.fullmatch(_CAPTION + r"\s+по\s+центру(?:\s+листа)?", text or ""):
        return "По центру"
    # Replacing the object does not discard any clause or rewrite any number.
    # The whole result must match an existing closed deterministic grammar.
    corrected = _correct_command_typos(prompt)
    effective = re.sub(r"\b" + _CAPTION + r"\b", "рисунок", corrected, count=1,
                       flags=re.IGNORECASE)
    if effective == prompt:
        return prompt
    effective = re.sub(r"\bна(?=[+-]?(?:\d|[.,]\d))", "на ", effective,
                       flags=re.IGNORECASE)
    parsed = parse(effective, {**state, "has_paths": True})
    if parsed.kind in {"move_preview", "rotate", "center", "place_preview", "mirror",
                       "margin", "resize_width"} or parsed.kind == "scale" and corrected != prompt:
        return effective
    return prompt


def _outside_quotes(prompt):
    return _QUOTE.sub(" __literal__ ", prompt)


def _caption_geometry_request(prompt):
    text = _short(_outside_quotes(prompt))
    if text is None:
        return False
    if _POSITION_FRAGMENT.fullmatch(_edit_request_text(text)):
        return True
    if not re.search(r"\b" + _CAPTION + r"\b", text):
        return False
    text = _edit_request_text(text)
    text = re.sub(r"^не\s+", "", text)
    if re.match(r"^(?:увеличь|уменьши|масштаб(?:ируй)?|сдвинь|перемести|перенеси|"
                r"поверни|разверни|отрази|выровняй|центрируй|отцентруй|впиши|"
                r"размести|помести|поставь|ширина|ширину)\b", text):
        return True
    if re.match(r"^сделай\b", text):
        return bool(re.search(r"\b(?:больше|меньше|крупнее|мельче|размер|ширину|поля)\b", text))
    return bool(re.fullmatch(_CAPTION + r"\s+по\s+центру(?:\s+листа)?", text))


def _explicit_whole_request(prompt):
    if not isinstance(prompt, str):
        return False
    text = _short(_outside_quotes(prompt))
    return bool(text and not re.search(r"\b" + _CAPTION + r"\b", text) and
                re.search(r"\b(?:рисунок|рисунка|эскиз|эскиза|изображение|изображения)\b", text))


def _whole_alias(prompt, state):
    if re.search(r"[«»“”\"']", prompt) or not _explicit_whole_request(prompt):
        return prompt
    effective = re.sub(r"\b(?:весь|целый)\s+(рисунок|эскиз)\b", r"\1", prompt,
                       flags=re.IGNORECASE)
    effective = re.sub(r"\b(?:все|всё|целое)\s+(изображение)\b", r"\1", effective,
                       flags=re.IGNORECASE)
    if effective != prompt and parse(effective, {**state, "has_paths": True}).requires_confirmation:
        return effective
    return prompt


def _chosen_caption(prompt, captions):
    text = _short(_outside_quotes(prompt)) or ""
    ordinal = re.search(r"\b(" + _ORDINAL + r")\s+" + _CAPTION + r"\b", text)
    if ordinal is None and _BARE_CHOICE.fullmatch(text):
        ordinal = re.match(r"(" + _ORDINAL + r")\b", text)
    if ordinal is not None and ordinal[1].startswith("последн"):
        return len(captions) - 1
    match = re.search(r"\bномер\s+([1-8])\b", text) or re.fullmatch(r"([1-8])", text)
    if match:
        index = int(match[1]) - 1
        return index if index < len(captions) else None
    for stem, index in _ORDINALS.items():
        if ordinal is not None and ordinal[1].startswith(stem):
            return index if index < len(captions) else None
    literals = [next(group for group in match.groups() if group is not None)
                for match in _QUOTE.finditer(prompt)]
    if len(literals) == 1:
        choices = [index for index, literal in enumerate(captions) if literal == literals[0]]
        if len(choices) == 1:
            return choices[0]
    return None


def _caption_scope_reply(prompt, state, target):
    if target is None or not target.captions:
        return None
    separate = len(target.captions) > 1 or target.other_geometry
    text = _short(prompt)
    choice = _BARE_CHOICE.fullmatch(text or "")
    retry = _RETRY.fullmatch(text or "") or _MORE_SCALE.fullmatch(text or "")
    retry_caption = bool(retry and not _explicit_whole_request(state.get("last_preview_request", "")))
    if not separate or not (_caption_geometry_request(prompt) or choice or retry_caption):
        return None
    selected = _chosen_caption(prompt, target.captions)
    if selected is not None:
        intro = f"Выбрана надпись {selected + 1}: «{target.captions[selected]}»."
    else:
        choices = "; ".join(f"{order}. «{literal[:60]}{'…' if len(literal) > 60 else ''}»"
                            for order, literal in enumerate(target.captions, 1))
        intro = ("В эскизе несколько надписей: " if len(target.captions) > 1 else
                 "Кроме надписи в эскизе есть другие линии: ") + choices + "."
        intro += " Уточните конкретную надпись либо явно весь рисунок."
    return (intro + " Выборочное перемещение, поворот и масштабирование одной надписи "
            "среди остальных элементов пока не поддерживаются; весь рисунок вместо неё не изменяю. "
            "Для отдельной надписи можно задать высоту, например «Высота первой надписи 8 мм». "
            "Для общего изменения напишите, например «Увеличь весь рисунок в 2 раза». Изменений нет.")


def normalize_followup(prompt, state):
    """Resolve bounded follow-ups from verified editor captions and scale.

    It always returns a string when given a string. Ambiguous, unsupported or
    unsafe requests are returned unchanged, with no dropped trailing clauses.
    A failed 10x attempt is NOT executed first; the new 5x remains relative to
    the unchanged, last valid editor proposal/current drawing.
    'Ещё в 5 раз' is a further transform; only a retry replaces a prior factor.
    """
    text = _short(prompt)
    if text is None or not isinstance(state, Mapping):
        return prompt
    target = _target(state)
    position = _POSITION_FRAGMENT.fullmatch(_edit_request_text(text))
    if (position is not None and position["distance"] is not None and
            target is not None and target.text is not None and not target.other_geometry):
        effective = f"сдвинь рисунок {position['direction']} на {position['distance']} мм"
        if parse(effective, {**state, "has_paths": True}).kind == "move_preview":
            return effective
    match = _RETRY.fullmatch(text) or _MORE_SCALE.fullmatch(text)
    if match is None:
        whole = _whole_alias(prompt, state)
        return whole if whole != prompt else _caption_alias(prompt, state, target)
    previous = _previous_scale(state, target)
    if previous is None:
        return prompt
    if ((len(target.captions) > 1 or target.other_geometry and target.captions) and
            not _explicit_whole_request(state.get("last_preview_request", ""))):
        return prompt
    direction = "увеличь" if previous.parameters["percent"] > 100 else "уменьши"
    noun = "надпись" if target.text is not None and not target.other_geometry else "рисунок"
    effective = f"{direction} {noun} в {match[1].strip()} раз"
    if parse(effective, {**state, "has_paths": True}).kind != "scale":
        return prompt
    return effective


def corrects_scale_request(prompt, state):
    """Only a terse retry replaces a factor; spelling/object aliases do not."""
    text = _short(prompt)
    return bool(_RETRY.fullmatch(text or "") and normalize_followup(prompt, state) != prompt)


def _scale_description(plan):
    percent = plan.parameters["percent"]
    if percent > 100:
        return f"увеличить в {percent / 100:g} раз"
    return f"уменьшить в {100 / percent:g} раз"


def target_reply(prompt, state):
    """Answer exact reference fragments locally; do not invent missing memory.

    None means this module does not recognize the fragment, not permission to
    perform an action. Returned text is a clarification, never a Plan.
    """
    text = _edit_request_text(_short(prompt) or "")
    target = _target(state) if isinstance(state, Mapping) else None
    if isinstance(prompt, str) and isinstance(state, Mapping):
        scoped = _caption_scope_reply(prompt, state, target)
        if scoped is not None:
            return scoped
    reference = _REFERENCE.fullmatch(text or "")
    retry = _RETRY.fullmatch(text or "") or _MORE_SCALE.fullmatch(text or "")
    missing_edit = _MISSING_EDIT.fullmatch(text or "")
    position = _POSITION_FRAGMENT.fullmatch(text)
    missing_position = position is not None and position["distance"] is None
    if reference is None and retry is None and missing_edit is None and not missing_position:
        return None
    if not isinstance(state, Mapping):
        return "Нет проверенного контекста надписи. Какой текст нужно изменить и во сколько раз? Изменений нет."
    previous = _previous_scale(state, target)
    if missing_edit is not None or missing_position:
        if target is None or target.text is None:
            return "Нет проверенного контекста конкретной надписи. Укажите текст и параметры изменения. Изменений нет."
        noun = f"надпись «{target.text}»"
        if missing_position:
            question = f"На сколько миллиметров сдвинуть {noun} {position['direction']}?"
        elif missing_edit[1]:
            question = f"На сколько миллиметров сдвинуть {noun} {missing_edit[1]}?"
        elif missing_edit[2]:
            question = f"На сколько градусов повернуть {noun}?"
        elif missing_edit[3] or missing_edit[4]:
            question = f"Во сколько раз изменить {noun} или какая нужна высота букв в миллиметрах?"
        else:
            question = f"Куда разместить {noun}: в каком углу или по центру?"
        return question + " Изменений нет."
    if retry is not None:
        if normalize_followup(prompt, state) != prompt:
            return None  # The normal action workflow can handle it.
        noun = f"надпись «{target.text}»" if target and target.text is not None else "рисунок"
        return f"Увеличить или уменьшить {noun}? Напишите направление и число, например «Увеличь надпись в 5 раз». Изменений нет."
    if target is None or target.text is None:
        return "Нет проверенного контекста конкретной надписи. Какой текст нужно изменить и во сколько раз? Изменений нет."
    location = "ещё не применённом предложении" if target.draft else "текущем эскизе"
    answer = f"Речь о надписи «{target.text}» в {location}."
    if previous is not None:
        answer += f" Последняя просьба — {_scale_description(previous)} относительно этого варианта."
        answer += " Укажите новый множитель или повторите эту команду; автоматически ничего не применено."
    else:
        answer += " Как изменить её размер: во сколько раз или какая высота букв в миллиметрах? Изменений нет."
    return answer
