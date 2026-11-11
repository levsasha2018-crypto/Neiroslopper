"""Exact string-only caption mutations, without plans, models or devices.

The editor replaces quoted literals with §n§ tokens before calling this parser.
The returned target must still be resolved against trusted caption metadata.
Font support, complete geometry and confirmation belong to the caller.
"""
from __future__ import annotations

import re
import unicodedata


MAX_TEXT = 800
_TOKEN = r"§\d+§"
_KEYS = {"operation", "fragment", "replacement", "all", "word"}
_OPERATIONS = {"prepend", "append", "replace", "delete"}


def _literal(token, literals, *, allow_empty=False):
    match = re.fullmatch(r"§(\d+)§", token)
    if not match or not isinstance(literals, (list, tuple)):
        raise ValueError("Нужен буквальный фрагмент в кавычках.")
    index = int(match[1])
    if index >= len(literals) or not isinstance(literals[index], str):
        raise ValueError("Не удалось прочитать фрагмент в кавычках.")
    literal = literals[index]
    if not (0 if allow_empty else 1) <= len(literal) <= MAX_TEXT:
        raise ValueError("Фрагмент должен содержать от 1 до 800 символов.")
    return literal


def _value(operation, fragment, replacement=None, *, all_matches=False, word=False):
    return {"operation": operation, "fragment": fragment, "replacement": replacement,
            "all": all_matches, "word": word}


def looks_like_request(masked_text):
    if not isinstance(masked_text, str):
        return False
    # Existing 'add frame', 'replace whole caption' and creation commands stay
    # with their own parsers; there is no broad verb-only interception.
    return bool(re.match(
        r"(?:добавь|вставь|допиши)\s+(?:§|(?:в|к|внутри)\b.*(?:начал|конец)|"
        r"(?:фрагмент|слово|строку)\b)|"
        r"(?:замени|поменяй|удали|убери)\s+(?:§|все\b|все\s+слова\b|"
        r"слово\b|слова\b|фрагмент\b|фрагменты\b|(?:в|из|внутри)\b.*§)|"
        r"(?:в|внутри)\s+.+\s+(?:замени|поменяй|удали|убери)\s+(?:§|все\b|слово\b|фрагмент\b)",
        masked_text))


def _target_pattern(pattern):
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 5000:
        raise ValueError("Некорректный выбор надписи.")
    # The main caption selector names grammatical text/word forms. Normalize
    # only the two locative nouns added for natural 'в тексте / в слове'; do
    # not weaken ordinal/literal selection or invent a target from prose.
    return pattern.replace("текст|", "(?:текст|тексте)|").replace("слово|", "(?:слово|слове)|")


def _target(value):
    if value is None:
        return None
    return re.sub(r"\bтексте\b", "текст", re.sub(r"\bслове\b", "слово", value))


def parse_request(masked_text, literals, target_pattern):
    """Return ('content', target, strict fields), None, or a clear ValueError.

    This parser consumes the full masked command. It never performs edits and
    never chooses among captions; quoted fragments retain their exact casing.
    """
    if not isinstance(masked_text, str) or not 1 <= len(masked_text) <= 1800:
        return None
    pattern = _target_pattern(target_pattern)
    try:
        # Added content is glued with one ASCII space unless a retained line
        # break/tab/other whitespace already separates the joined edges.
        # Only ASCII spaces at the join are deliberately normalized.
        additions = [
            r"(?:добавь|вставь|допиши)\s+(?:фрагмент\s+|слово\s+)?(?P<new>" + _TOKEN + r")\s+в\s+(?P<where>начало|конец)(?:\s+(?P<target>" + pattern + r"))?",
            r"(?:добавь|вставь|допиши)\s+в\s+(?P<where>начало|конец)(?:\s+(?P<target>" + pattern + r"))?\s+(?:фрагмент\s+|слово\s+)?(?P<new>" + _TOKEN + r")",
            r"(?:добавь|вставь|допиши)\s+к\s+(?P<target>" + pattern + r")\s+в\s+(?P<where>начало|конец)\s+(?P<new>" + _TOKEN + r")",
        ]
        for expression in additions:
            match = re.fullmatch(expression, masked_text)
            if match:
                fragment = _literal(match["new"], literals)
                if not fragment.strip():
                    raise ValueError("Добавляемый фрагмент не должен состоять только из пробелов.")
                operation = "prepend" if match["where"] == "начало" else "append"
                return "content", _target(match["target"]), _value(operation, fragment)

        # Explicit 'слово' chooses Unicode word boundaries; 'фрагмент' and no
        # noun choose an exact substring. 'все' is the sole all-match authority.
        selection = r"(?:(?P<all>все)\s+)?(?:(?P<scope>слово|слова|фрагмент|фрагменты)\s+)?(?P<old>" + _TOKEN + r")"
        replace_body = r"(?:замени|поменяй)\s+" + selection + r"\s+на\s+(?P<new>" + _TOKEN + r")"
        delete_body = r"(?:удали|убери)\s+" + selection
        for operation, body in (("replace", replace_body), ("delete", delete_body)):
            verb = r"(?:замени|поменяй)" if operation == "replace" else r"(?:удали|убери)"
            suffix = selection + (r"\s+на\s+(?P<new>" + _TOKEN + r")" if operation == "replace" else "")
            expressions = [body + r"(?:\s+(?:в|из|внутри)\s+(?P<target>" + pattern + r"))?",
                           r"(?:в|внутри)\s+(?P<target>" + pattern + r")\s+" + body,
                           verb + r"\s+(?:в|из|внутри)\s+(?P<target>" + pattern + r")\s+" + suffix]
            for expression in expressions:
                match = re.fullmatch(expression, masked_text)
                if not match:
                    continue
                fragment = _literal(match["old"], literals)
                replacement = _literal(match["new"], literals, allow_empty=True) if operation == "replace" else None
                scope = match["scope"] or ""
                return "content", _target(match["target"]), _value(
                    operation, fragment, replacement, all_matches=match["all"] is not None,
                    word=scope in {"слово", "слова"})
    except re.error:
        raise ValueError("Некорректный выбор надписи.") from None
    if looks_like_request(masked_text):
        raise ValueError("Уточните одну правку: «Добавь «Ёж» в конец надписи», "
                         "«Замени слово «Марина» на «Анна» в надписи» или "
                         "«Удали фрагмент «...» из надписи». Для нескольких совпадений явно укажите «все».")
    return None


def _word_character(character):
    return character.isalnum() or character == "_" or unicodedata.category(character).startswith("M")


def _positions(current, fragment, *, word):
    if word and not all(_word_character(character) for character in fragment):
        raise ValueError("Для правки целого слова укажите одно слово без пробелов и знаков; иначе используйте «фрагмент».")
    found, start = [], 0
    while True:
        position = current.find(fragment, start)
        if position < 0:
            break
        end = position + len(fragment)
        if not word or ((position == 0 or not _word_character(current[position - 1])) and
                        (end == len(current) or not _word_character(current[end]))):
            found.append((position, end))
        start = position + 1
    return found


def rewrite(current_literal, value):
    """Return a new string or reject; never mutate layout or partial content.

    Matching is case-sensitive, exact and Unicode-aware for explicit words.
    Added content gets one separating ASCII space, except at a retained LF,
    CR, tab or other whitespace boundary. Deletion keeps the remaining
    whitespace exactly; no punctuation cleanup, automatic spelling fixes or
    case conversion is inferred.
    """
    if not isinstance(current_literal, str) or not 1 <= len(current_literal) <= MAX_TEXT or not current_literal.strip():
        raise ValueError("Нет корректного исходного текста до 800 символов.")
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ValueError("Некорректные поля текстовой правки.")
    operation, fragment, replacement = value["operation"], value["fragment"], value["replacement"]
    if (not isinstance(operation, str) or operation not in _OPERATIONS or
            not isinstance(fragment, str) or not 1 <= len(fragment) <= MAX_TEXT or
            not isinstance(value["all"], bool) or not isinstance(value["word"], bool)):
        raise ValueError("Нужны известная правка и непустой буквальный фрагмент до 800 символов.")
    if operation == "replace":
        if not isinstance(replacement, str) or not 0 <= len(replacement) <= MAX_TEXT:
            raise ValueError("Нужен буквальный новый фрагмент до 800 символов.")
    elif replacement is not None:
        raise ValueError("Лишний новый фрагмент в правке.")
    if operation in {"prepend", "append"}:
        if value["all"] or value["word"] or not fragment.strip():
            raise ValueError("Для добавления нужен непустой фрагмент без поиска слов или повторов.")
        left, right = (fragment, current_literal) if operation == "prepend" else (current_literal, fragment)
        left, right = left.rstrip(" "), right.lstrip(" ")
        separator = "" if left[-1:].isspace() or right[:1].isspace() else " "
        result = left + separator + right
    else:
        matches = _positions(current_literal, fragment, word=value["word"])
        if not matches:
            raise ValueError("Указанный фрагмент не найден с точно таким регистром; текст не изменён.")
        if len(matches) > 1 and not value["all"]:
            raise ValueError("Фрагмент встречается несколько раз. Уточните уникальный фрагмент или явно укажите «все».")
        if any(previous[1] > following[0] for previous, following in zip(matches, matches[1:])):
            raise ValueError("Совпадения пересекаются. Уточните непересекающийся фрагмент; текст не изменён.")
        inserted = replacement if operation == "replace" else ""
        pieces, previous = [], 0
        for start, end in matches:
            pieces.extend((current_literal[previous:start], inserted))
            previous = end
        pieces.append(current_literal[previous:])
        result = "".join(pieces)
    if not result.strip():
        raise ValueError("Правка удаляет весь текст. Для очистки эскиза нужна отдельная команда; надпись не изменена.")
    if len(result) > MAX_TEXT:
        raise ValueError("После правки больше 800 символов. Сократите фрагмент; надпись не изменена.")
    return result
