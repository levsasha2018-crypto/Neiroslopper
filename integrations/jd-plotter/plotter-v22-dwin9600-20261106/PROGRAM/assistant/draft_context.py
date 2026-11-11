"""Actual editor/draft context, not invented conversational memory."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .planner import Plan, transform_paths


def operations(plan):
    if not isinstance(plan, Plan):
        raise ValueError("Нет проверенного плана.")
    return deepcopy(plan.parameters["operations"] if plan.kind == "free_preview" else
                    [{"kind": plan.kind, "parameters": plan.parameters}])


def plan_anchor(plan, previous=None):
    anchor = previous
    for item in operations(plan):
        kind, p = item["kind"], item["parameters"]
        if kind in {"print_text", "place_preview", "scale"} and "anchor" in p:
            anchor = p["anchor"]
        elif kind == "text":
            # The fixed handwritten sample is imported with a 3 mm top-left
            # margin, unlike centered generated block letters.
            anchor = "top_left"
        elif kind in {"center", "margin", "ruler", "test_pattern", "print_text"}:
            anchor = "center"
        elif kind in {"polyline", "ellipse", "append_text", "object_scene", "text_block"}:
            anchor = None
    return anchor


def relative_request(prompt):
    text = re.sub(r"\s+", " ", prompt.casefold().replace("ё", "е")).strip()
    if re.search(r"\b(?:напиши|напечатай|нарисуй|создай|построй|изобрази)\b", text):
        return False
    from .creative_planner import edits_existing_text
    if edits_existing_text(prompt):
        return True
    return bool(re.match(r"(?:(?:пожалуйста|давай|можешь)\s+)?(?:увелич|уменьш|повер|развер|"
                         r"сдвин|перенес|отраз|выров|впиш|размест|помест|по центру|ширина|"
                         r"добавь рамку|сделай\b.{0,70}(?:больш|меньш|крупн|мелк|размер|ширин)|"
                         r"(?:сделай|оформи|укрась)\s+(?:эту\s+)?(?:надпись|текст|слово)\b)", text))


def resolve(prompt, state):
    current = dict(state)
    draft = current.get("preview_draft")
    if not isinstance(draft, Plan) or not relative_request(prompt):
        if isinstance(prompt, str) and re.search(r"\b(?:напиши|напечатай|нарисуй|создай|построй|изобрази)\b",
                                               prompt.casefold()):
            current.pop("preview_draft", None)
        return current, []
    current.pop("preview_draft", None)
    prefix = operations(draft)
    if len(prefix) >= 8:
        raise ValueError("В предложении уже восемь шагов. Сначала примените или отмените его.")
    width, height = current.get("sheet_width"), current.get("sheet_height")
    result = transform_paths(current.get("editor_paths", ()), draft, width, height)
    current["editor_paths"] = result
    current["has_paths"] = bool(result)
    current["preview_anchor"] = plan_anchor(draft, current.get("preview_anchor"))
    # The renderer now sees the computed draft geometry, while read-only and
    # creative contexts retain its actual typography. No assistant prose is
    # allowed to substitute for the generator. A transform-only draft can
    # inherit the editor's existing typed task without inventing any literal.
    current["preview_task"] = applied_task(draft, current.get("preview_task"))
    current["using_draft"] = True
    return current, prefix


def chain(prefix, plan):
    if not prefix:
        return plan
    steps = [*prefix, *operations(plan)]
    if len(steps) > 8:
        raise ValueError("Слишком много изменений в одном предложении. Сначала примените или отмените его.")
    original = []
    for i, item in enumerate(prefix, 1):
        params = item["parameters"]
        if item["kind"] == "polyline":
            params = {**params, "points": f"{len(params['points'])} точек на предпросмотре"}
        original.append(f"{i}. {item['kind']}: " + json.dumps(params, ensure_ascii=False, allow_nan=False))
    explanation = ("Продолжение ещё не применённого предложения. Сначала:\n" +
                   "\n".join(original) + "\nЗатем:\n" + plan.explanation)
    return Plan("free_preview", {"operations": steps}, explanation)


def applied_task(plan, previous=None):
    """Keep typography metadata when a current applied drawing is transformed."""
    if plan.kind in {'object_scene', 'text_block'}:
        return plan
    if isinstance(previous, Plan) and previous.kind in {'object_scene', 'text_block'}:
        # Studio handles object-aware edits itself. Never put a scene inside an
        # unsupported legacy program or pretend unknown geometry retains text.
        return plan if plan.kind in {'print_text', 'text', 'ruler', 'test_pattern'} else None
    steps = operations(plan)
    replacing = any(item["kind"] in {"print_text", "text", "ruler", "test_pattern"} or
                    item["kind"] in {"polyline", "ellipse"} and item["parameters"]["mode"] == "replace"
                    for item in steps)
    if replacing or not isinstance(previous, Plan):
        return plan
    compact = []
    for step in [*operations(previous), *steps]:
        if compact and step["kind"] == compact[-1]["kind"]:
            kind, a, b = step["kind"], compact[-1]["parameters"], step["parameters"]
            if kind == "scale" and a.get("anchor", "center") == b.get("anchor", "center"):
                percent = a["percent"] * b["percent"] / 100
                if 0 < percent <= 10000:
                    compact[-1] = {"kind": kind, "parameters": {**b, "percent": percent}}
                    continue
            elif kind == "move_preview":
                compact[-1] = {"kind": kind, "parameters": {
                    "dx_mm": a["dx_mm"] + b["dx_mm"], "dy_mm": a["dy_mm"] + b["dy_mm"]}}
                continue
            elif kind == "center":
                continue
        compact.append(step)
    if len(compact) > 8:
        return None
    return Plan("free_preview", {"operations": compact}, plan.explanation)
