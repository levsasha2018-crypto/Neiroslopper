"""Small read-only chat context, kept separate from action authorization.

Conversation strings are remembered as strings, never parsed into trusted
editor facts. Text context comes only from actual Plan metadata supplied by the
editor. No geometry, arbitrary objects, files, models or controllers are read.
"""
from __future__ import annotations

import json
import math
from typing import Mapping

from .planner import Plan


MAX_HISTORY_PAIRS = 4
MAX_HISTORY_CHARACTERS = 2300
MAX_SNAPSHOT_CHARACTERS = 4800
_ANCHORS = {"center", "top_left", "top_right", "bottom_left", "bottom_right"}
_BOOLEAN_FIELDS = {"busy", "sandbox", "connected", "simulation", "has_paths", "using_draft",
                   "pen_lift_enabled", "pen_lift_ready", "pen_power_raises", "pen_continuous",
                   "controller_config_verified", "panel_reply", "native_panel", "panel_assistant"}
_NUMBER_FIELDS = {
    "sheet_width": (1, 3000), "sheet_height": (1, 3000),
    "drawing_width": (0, 3000), "drawing_height": (0, 3000),
    "contour_length_mm": (0, 1e12), "connection_length_mm": (0, 1e12),
    "travel_length_mm": (0, 1e12), "feed_mm_min": (0, 1e9),
    "feed_percent": (0, 100), "max_feed_mm_min": (0, 1e9),
}
_COUNT_FIELDS = {"path_count", "point_count", "connection_count"}
_PARAMETERS = {
    "center": set(), "rotate": {"degrees"}, "scale": {"percent"},
    "resize_width": {"width_mm"}, "margin": {"margin_mm"}, "mirror": {"axis"},
    "move_preview": {"dx_mm", "dy_mm"}, "place_preview": {"anchor", "margin_mm"},
    "print_text": {"text", "height_mm", "spacing_mm"},
    "append_text": {"text", "height_mm", "spacing_mm"}, "text": {"text", "height_mm"},
    "frame": {"padding_mm"}, "ruler": {"length_mm"}, "test_pattern": {"size_mm"},
    "polyline": {"points", "mode"}, "ellipse": {"cx", "cy", "rx", "ry", "mode"},
}
_NUMERIC_PARAMETERS = {
    "degrees": (-36000, 36000), "percent": (0, 10000),
    "width_mm": (0, 3000), "margin_mm": (0, 1500),
    "dx_mm": (-3000, 3000), "dy_mm": (-3000, 3000),
    "height_mm": (1, 100), "spacing_mm": (0, 100), "padding_mm": (0, 1500),
    "length_mm": (0, 3000), "size_mm": (0, 3000),
    "cx": (0, 3000), "cy": (0, 3000), "rx": (0, 3000), "ry": (0, 3000),
}
_POSITIVE_PARAMETERS = {"percent", "width_mm", "length_mm", "size_mm", "rx", "ry"}


def _clip(text, limit):
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def bounded_history(history) -> tuple:
    """Return recent complete string pairs; neither role becomes an authority.

Both messages remain present when a long pair is shortened. Malformed roles,
objects and blank turns are dropped. At most the most recent 64 entries are
examined, so an old huge transcript cannot create a huge intermediate copy.
"""
    if not isinstance(history, (list, tuple)):
        return ()
    selected = []
    used = 0
    for pair in reversed(history[-64:]):
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or
                not all(isinstance(part, str) and part.strip() for part in pair)):
            continue
        user, assistant = _clip(pair[0], 400), _clip(pair[1], 500)
        remaining = MAX_HISTORY_CHARACTERS - used
        if remaining < 2:
            break
        if len(user) + len(assistant) > remaining:
            # Keep a complete pair rather than a lone assistant answer.
            user_limit = max(1, min(len(user), remaining * 4 // 9))
            assistant_limit = max(1, min(len(assistant), remaining - user_limit))
            user = _clip(user, user_limit)
            assistant = _clip(assistant, assistant_limit)
        selected.append((user, assistant))
        used += len(user) + len(assistant)
        if len(selected) == MAX_HISTORY_PAIRS:
            break
    return tuple(reversed(selected))


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and low <= value <= high
    except (OverflowError, TypeError, ValueError):
        return False


def _operation(item):
    if (not isinstance(item, dict) or len(item) != 2 or set(item) != {"kind", "parameters"} or
            not isinstance(item["kind"], str) or item["kind"] not in _PARAMETERS or
            not isinstance(item["parameters"], dict)):
        return None
    kind, parameters = item["kind"], item["parameters"]
    if len(parameters) > 8:
        return None
    keys, required = set(parameters), _PARAMETERS[kind]
    if keys != required:
        if kind == "scale" and keys == required | {"anchor"}:
            pass
        elif kind in {"print_text", "append_text"} and keys == required | {"anchor", "margin_mm"}:
            pass
        else:
            return None
    copied = {}
    for name, value in parameters.items():
        if name == "text":
            if not isinstance(value, str) or not value.strip() or not 1 <= len(value) <= 800:
                return None
            copied[name] = _clip(value, 400)
            if len(value) > 400:
                copied["text_truncated"] = True
        elif name == "anchor":
            if not isinstance(value, str) or value not in _ANCHORS:
                return None
            copied[name] = value
        elif name == "axis":
            if not isinstance(value, str) or value not in {"horizontal", "vertical"}:
                return None
            copied[name] = value
        elif name == "mode":
            if not isinstance(value, str) or value not in {"append", "replace"}:
                return None
            copied[name] = value
        elif name == "points":
            if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 1000:
                return None
            if any(not isinstance(point, (list, tuple)) or len(point) != 2 or
                   not all(_number(coordinate, 0, 3000) for coordinate in point)
                   for point in value):
                return None
            # These are described, never copied or interpreted as current
            # machine position. No coordinate list is sent to the chat model.
            copied["point_count"] = len(value)
        elif name == "spacing_mm" and value is None:
            copied[name] = None
        elif name in _NUMERIC_PARAMETERS:
            if not _number(value, *_NUMERIC_PARAMETERS[name]) or (name in _POSITIVE_PARAMETERS and value <= 0):
                return None
            copied[name] = value
        else:
            return None
    return {"kind": kind, "parameters": copied}


def _text_context(value, source, selected=None):
    if not isinstance(value, Plan) or not isinstance(value.parameters, dict):
        return None
    if value.kind=='object_scene':
        scene=value.parameters.get('scene')
        if set(value.parameters)!={'scene'} or not isinstance(scene,dict) or set(scene)!={'version','objects','route'} or type(scene['version']) is not int or scene['version']!=1 or not isinstance(scene['objects'],list) or not 1<=len(scene['objects'])<=100:
            return None
        captions=[]
        for obj in scene['objects']:
            if not isinstance(obj,dict) or set(obj)!={'id','name','source','matrix'} or not isinstance(obj['id'],str) or not isinstance(obj['source'],dict) or set(obj['source'])!={'kind','parameters'}:
                return None
            child=_text_context(Plan(obj['source']['kind'],obj['source']['parameters']),source) if obj['source']['kind']!='object_scene' else None
            if child:
                captions.extend({**item,'object_id':obj['id']} for item in child['captions'])
        if not captions:
            return None
        if selected:
            captions.sort(key=lambda item: item.get('object_id') != selected)
        return {'source':source,'caption_count':len(captions),'captions':captions[:4],
                'transforms':[],'sizes_are_original_program_inputs':True,'not_authorization_to_apply':True,
                'object_transforms_not_reported':True,'captions_truncated':len(captions)>4}
    if value.kind=='text_block':
        p=value.parameters
        required={'text','width_mm','height_mm','font_height_mm','spacing_mm','line_spacing','align'}
        if set(p)!=required or not _number(p['width_mm'],1,3000) or not _number(p['height_mm'],1,3000) or not _number(p['line_spacing'],1.4,5) or not isinstance(p['align'],str) or p['align'] not in {'left','center','right'}:
            return None
        value=Plan('print_text',{'text':p['text'],'height_mm':p['font_height_mm'],'spacing_mm':p['spacing_mm']})
    if value.kind == "free_preview":
        if len(value.parameters) != 1 or set(value.parameters) != {"operations"}:
            return None
        raw = value.parameters["operations"]
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 8:
            return None
    else:
        raw = [{"kind": value.kind, "parameters": value.parameters}]
    descriptions = [_operation(item) for item in raw]
    if any(item is None for item in descriptions):
        return None
    captions, transforms = [], []
    for step, item in enumerate(descriptions):
        kind, parameters = item["kind"], item["parameters"]
        if kind in {"print_text", "text"}:
            captions = [{"step": step, "kind": kind, "parameters": parameters}]
            transforms = []
        elif kind == "append_text":
            captions.append({"step": step, "kind": kind, "parameters": parameters})
        elif kind in {"ruler", "test_pattern"} or (
                kind in {"polyline", "ellipse"} and parameters["mode"] == "replace"):
            captions, transforms = [], []
        else:
            transforms.append({"step": step, **item})
    if not captions:
        return None
    result = {"source": source, "caption_count": len(captions), "captions": captions[:4],
              "transforms": transforms,
              "sizes_are_original_program_inputs": True,
              "not_authorization_to_apply": True}
    if len(captions) > 4:
        result["captions_truncated"] = True
    return result


def chat_snapshot(state) -> dict:
    """Copy only finite, typed editor facts and actual text-plan metadata.

In particular, paths, previous answers, arbitrary mappings and Plan explanations
are not evidence and never enter the returned JSON-safe object. Scaling remains
an operation parameter; this helper does not invent the current font height.
"""
    if not isinstance(state, Mapping):
        return {}
    result = {}
    from machine_profiles import UNO, DLC32, assistant_facts
    from .project_answers import uses_v22
    if state.get("machine_profile") in (UNO, DLC32):
        result.update(assistant_facts(state["machine_profile"]))
    if uses_v22(state):
        result.update(board_revision="2.2", controller_board="MKS DLC32 V2.2",
                      motor_socket_roles="X carriage on Y1; Y portal on X + Z (second motor inverted)",
                      pen_output_profile="GPIO32 LOW activates relay; M8 power ON; M9 OFF",
                      endstop_configuration="no limit switches; hard limits and homing disabled",
                      dwin_connection="DWIN at 9600 8N1, TX GPIO23/RX GPIO33; TCP 8888 DGUS; TCP 23 motion")
    if state.get("display_model") == "DMG80480C043_01WTC":
        result["display_model"] = "DMG80480C043_01WTC"
    for key in sorted(_BOOLEAN_FIELDS):
        value = state.get(key)
        if isinstance(value, bool):
            result[key] = value
    for key, limits in _NUMBER_FIELDS.items():
        value = state.get(key)
        if _number(value, *limits):
            result[key] = value
    for key in sorted(_COUNT_FIELDS):
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1e9:
            result[key] = value
    for key in ("file_name", "name"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = _clip(value, 400)
    draft = state.get("preview_draft")
    if isinstance(draft, Plan):
        context = _text_context(draft, "preview_draft", state.get('studio_selected'))
    else:
        context = _text_context(state.get("preview_task"), "preview_task", state.get('studio_selected'))
    if context is not None:
        result["current_text"] = context
        selected=state.get('studio_selected')
        if isinstance(selected,str) and any(item.get('object_id')==selected for item in context['captions']):
            context['selected_object_id']=selected
    # Typical snapshots are far smaller. Escaped control characters can make
    # the JSON longer than its string fields; reduce only descriptive payloads.
    while len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > MAX_SNAPSHOT_CHARACTERS:
        context = result.get("current_text")
        if context and len(context["captions"]) > 1:
            context["captions"].pop()
            context["captions_truncated"] = True
        elif context and len(context["captions"][0]["parameters"]["text"]) > 80:
            parameters = context["captions"][0]["parameters"]
            parameters["text"] = _clip(parameters["text"], 80)
            parameters["text_truncated"] = True
        elif "name" in result:
            del result["name"]
        elif "file_name" in result:
            del result["file_name"]
        else:
            # This cannot occur with the bounded numeric/operation schema, but
            # a context-free snapshot is preferable to oversized chat input.
            result.pop("current_text", None)
            break
    from .task_memory import clean
    memory = clean(state.get('task_memory'))
    if memory:
        # Prefer the active task to the old transcript. Bound the combined JSON
        # without dropping the selected caption or current request.
        memory['events'] = []
        memory['request'] = memory['request'][:300]
        result['task_memory'] = memory
        while len(json.dumps(result, ensure_ascii=False)) > MAX_SNAPSHOT_CHARACTERS:
            captions = result.get('current_text', {}).get('captions', [])
            if len(captions) > 1:
                captions.pop(); result['current_text']['captions_truncated'] = True
            elif 'file_name' in result:
                result.pop('file_name')
            elif 'name' in result:
                result.pop('name')
            elif captions and len(captions[0]['parameters']['text']) > 80:
                captions[0]['parameters']['text'] = _clip(captions[0]['parameters']['text'], 80)
                captions[0]['parameters']['text_truncated'] = True
            else:
                result.pop('current_text', None)
                break
    return result
