from copy import deepcopy
from .planner import Plan

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
