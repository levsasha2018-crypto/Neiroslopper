"""Serialize confirmed preview paths as SVG, no image/font/code references."""
from html import escape
from pathlib import Path


def render_svg(paths, width_mm, height_mm, title="Эскиз плоттера"):
    from geometry import Sheet, validate_paths
    sheet = Sheet(width_mm, height_mm)
    validate_paths(paths, sheet)
    if not paths:
        raise ValueError("Нет контуров для сохранения.")
    if sum(len(path) for path in paths) > 50_000:
        raise ValueError("Слишком много точек для SVG.")
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{sheet.width:g}mm" '
             f'height="{sheet.height:g}mm" viewBox="0 0 {sheet.width:g} {sheet.height:g}" '
             'data-plotter-size="actual">', f'<title>{escape(str(title)[:200])}</title>']
    for path in paths:
        coordinates = " ".join(("M" if index == 0 else "L") + f"{x:.8f},{y:.8f}"
                               for index, (x, y) in enumerate(path))
        lines.append(f'<path d="{coordinates}" fill="none" stroke="#163b60" stroke-width="0.18"/>')
    lines.append("</svg>")
    result = "\n".join(lines) + "\n"
    if len(result.encode("utf-8")) > 3_000_000:
        raise ValueError("SVG превышает предел 3 МБ.")
    return result


def save_svg(path, paths, width_mm, height_mm, title="Эскиз плоттера"):
    # Prepare/validate before opening the explicitly selected destination.
    content = render_svg(paths, width_mm, height_mm, title)
    Path(path).write_text(content, encoding="utf-8")
