"""Read-only reference search and verified first-party artwork downloads."""
from dataclasses import dataclass, asdict, replace
from html import unescape
import hashlib
import json
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

API = 'https://commons.wikimedia.org/w/api.php'
MIPT_SITE = 'https://mipt.ru/'
MIPT_LOGOS = MIPT_SITE + 'api/v1/?action=logo.getItemsShort&lang=ru'
_MIPT_ARTWORK_PATH = re.compile(r'/upload/iblock/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+\.svg\Z')
MAX_DOWNLOAD = 8_000_000
USER_AGENT = 'PlotterReference/1.0 (desktop reference selection; no uploads)'


class ReferenceCancelled(ValueError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ReferenceCancelled('Операция отменена.')


def checked_url(url, *, official=False):
    try:
        parts = urlsplit(url)
        hosts = ('mipt.ru',) if official else ('commons.wikimedia.org', 'upload.wikimedia.org', 'thumb.wikimedia.org')
        if (parts.scheme != 'https' or parts.hostname not in hosts or parts.username or
                parts.password or parts.port not in (None, 443) or parts.fragment):
            raise ValueError()
        if official and url != MIPT_LOGOS and (parts.query or not _MIPT_ARTWORK_PATH.fullmatch(parts.path)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Недопустимый адрес на официальном сайте МФТИ.' if official else
                         'Источник должен быть HTTPS-адресом Wikimedia Commons.') from None
    return url


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, official=False):
        self.official = official

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl, official=self.official)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, *, limit=MAX_DOWNLOAD, cancel=None, timeout=12, official=False):
    check_cancel(cancel)
    checked_url(url, official=official)
    try:
        opener = build_opener(SafeRedirect(official))
        with opener.open(Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'application/json,image/*;q=0.9'}), timeout=timeout) as response:
            checked_url(response.geturl(), official=official)
            length = response.headers.get('Content-Length')
            if length and int(length) > limit:
                raise ValueError('Источник слишком большой; выберите другой образец.')
            chunks, size = [], 0
            deadline = time.monotonic() + 35
            while True:
                check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise ValueError('Источник слишком долго отвечает; повторите поиск.')
                data = response.read(min(65536, limit + 1 - size))
                if not data:
                    break
                chunks.append(data)
                size += len(data)
                if size > limit:
                    raise ValueError('Источник слишком большой; выберите другой образец.')
            check_cancel(cancel)
            if not size:
                raise ValueError('Источник прислал пустой файл.')
            return b''.join(chunks)
    except ReferenceCancelled:
        raise
    except HTTPError as exc:
        exc.close()
        if exc.code == 429:
            if official:
                raise ValueError('Сайт МФТИ временно ограничил загрузку. Повторите запрос позже.') from exc
            raise ValueError('Wikimedia временно ограничила загрузки. Подождите перед повтором или откройте локальную картинку; автоматических повторов нет.') from exc
        raise ValueError('Источник недоступен. Выберите другой образец или откройте локальную картинку.') from exc
    except (OSError, TimeoutError) as exc:
        if official:
            raise ValueError('Не удалось загрузить исходник с сайта МФТИ. Повторите запрос позже.') from exc
        raise ValueError('Не удалось связаться с Wikimedia Commons. Можно повторить поиск или открыть локальную картинку.') from exc


def plain(value, length=300):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', unescape(str(value)))).strip()[:length]


@dataclass(frozen=True)
class Candidate:
    title: str
    url: str
    thumbnail: str
    page: str
    mime: str
    author: str = ''
    license: str = ''
    origin: str = 'commons'


def first_mipt_frame(raw):
    """Freeze only the first logo in the site's animated header artwork."""
    if re.search(rb'<!\s*(?:doctype|entity)', raw, re.I):
        raise ValueError('Логотип содержит неподдерживаемую структуру SVG.')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('Сайт МФТИ вернул повреждённый SVG.') from exc
    namespace = '{http://www.w3.org/2000/svg}'
    if root.tag != namespace + 'svg':
        raise ValueError('Официальный файл не является SVG.')
    groups = [item for item in root if item.tag == namespace + 'g' and item.get('class') == 'file-1']
    if not groups:
        return raw  # The ordinary importer validates static artwork.
    view_box = root.get('viewBox')
    if len(groups) != 1 or not view_box or set(groups[0].attrib) != {'class'}:
        raise ValueError('Формат первого кадра логотипа изменился.')
    first = ET.Element('svg', {'xmlns': namespace[1:-1], 'viewBox': view_box})
    for item in groups[0]:
        attribute = {namespace + 'path': 'd', namespace + 'polyline': 'points'}.get(item.tag)
        if not attribute or not item.get(attribute) or len(item) or set(item.attrib) - {attribute, 'class'}:
            raise ValueError('Формат контуров логотипа изменился.')
        ET.SubElement(first, item.tag[len(namespace):], {attribute: item.get(attribute)})
    if not len(first):
        raise ValueError('В первом кадре логотипа нет контуров.')
    return ET.tostring(first, encoding='utf-8')


def official_mipt_logo(cancel=None):
    """Use the same Russian header-logo list as the current home page."""
    try:
        rows = json.loads(download(MIPT_LOGOS, limit=64_000, cancel=cancel, official=True))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError('Сайт МФТИ вернул повреждённый список логотипов.') from exc
    if not isinstance(rows, list):
        raise ValueError('Сайт МФТИ изменил список логотипов.')
    for row in rows:
        if not isinstance(row, dict) or row.get('isWhite') or row.get('isFooter'):
            continue
        file = row.get('file')
        source = file.get('src') if isinstance(file, dict) else None
        if not isinstance(source, str) or not _MIPT_ARTWORK_PATH.fullmatch(source):
            continue
        url = MIPT_SITE.rstrip('/') + source
        candidate = Candidate('Официальный логотип МФТИ', url, url, MIPT_SITE, 'image/svg+xml',
                              'МФТИ', 'Условия использования указаны у правообладателя', 'mipt')
        return candidate, download(url, limit=64_000, cancel=cancel, official=True)
    raise ValueError('На сайте МФТИ не найден исходный векторный логотип.')


def _candidates(data):
    rows = []
    query = data.get('query', {})
    if not isinstance(query, dict):
        return rows
    pages = query.get('pages', {})
    for page in pages.values() if isinstance(pages, dict) else []:
        if not isinstance(page, dict):
            continue
        info = page.get('imageinfo', [])
        if not isinstance(info, list) or not info or not isinstance(info[0], dict):
            continue
        item = info[0]
        mime = item.get('mime', '')
        if mime not in ('image/svg+xml', 'image/png', 'image/jpeg', 'image/webp', 'image/gif'):
            continue
        try:
            original = checked_url(item.get('url', ''))
            thumb = checked_url(item.get('thumburl', '') or original)
            source = checked_url(item.get('descriptionurl', ''))
        except ValueError:
            continue
        metadata = item.get('extmetadata', {})
        metadata = metadata if isinstance(metadata, dict) else {}
        def value(key, default=''):
            field = metadata.get(key, {})
            return plain(field.get('value', default)) if isinstance(field, dict) else default
        author = value('Artist')
        license_name = value('LicenseShortName', 'Не указана — проверьте источник')
        title = plain(page.get('title', 'Образец')).removeprefix('File:')
        rows.append(Candidate(title,
                              original, thumb, source, mime, author, license_name))
    return rows


def search(query, *, cancel=None, limit=6):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 180 or any(ord(c) < 32 for c in query):
        raise ValueError('Введите название предмета длиной 2–180 знаков.')
    if type(limit) is not int or not 1 <= limit <= 12:
        raise ValueError('Недопустимое число образцов.')
    query = query.strip()
    rows = {}
    # First seek drawings, then fall back to photographs. SVGs sort first,
    # but neither filenames nor this ranking claim to verify artistic quality.
    for terms in (query + ' drawing', query):
        check_cancel(cancel)
        params = {'action': 'query', 'generator': 'search', 'gsrsearch': terms,
                  'gsrnamespace': 6, 'gsrlimit': 12, 'prop': 'imageinfo',
                  'iiprop': 'url|mime|extmetadata', 'iiurlwidth': 1200, 'format': 'json'}
        raw = download(API + '?' + urlencode(params), limit=2_000_000, cancel=cancel)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('Поиск вернул повреждённый ответ.') from None
        if not isinstance(data, dict) or 'error' in data:
            raise ValueError('Сервис поиска отклонил запрос. Попробуйте более короткое название.')
        for row in _candidates(data):
            rows.setdefault(row.url, row)
        if sum(row.mime == 'image/svg+xml' for row in rows.values()) >= 3:
            break
    def rank(row):
        return (row.mime != 'image/svg+xml',
                not bool(re.search('drawing|diagram|sketch|line|profile|silhouette', row.title, re.I)))
    return sorted(rows.values(), key=rank)[:limit]


def save_source(directory, raw, candidate):
    """Content-addressed selected source and attribution; thumbnails stay in RAM."""
    if directory is None:
        return
    if not isinstance(candidate, Candidate) or not raw or len(raw) > MAX_DOWNLOAD:
        raise ValueError('Некорректный источник образца.')
    folder = Path(directory) / 'references'
    folder.mkdir(parents=True, exist_ok=True)
    # Identical pixels can have different sources/licenses: never overwrite attribution.
    metadata = json.dumps(asdict(candidate), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw + metadata.encode('utf-8')).hexdigest()
    suffix = '.svg' if candidate.mime == 'image/svg+xml' else '.image'
    (folder / (digest + suffix)).write_bytes(raw)
    (folder / (digest + '.source.json')).write_text(
        json.dumps(asdict(candidate), ensure_ascii=False, indent=2), encoding='utf-8')


_ALIASES = {'порше': 'Porsche', 'машину': 'car', 'автомобиль': 'car', 'ракету': 'rocket',
            'кошку': 'cat', 'кота': 'cat', 'собаку': 'dog', 'самолет': 'airplane',
            'самолёт': 'airplane', 'мотоцикл': 'motorcycle', 'бабочку': 'butterfly', 'розу': 'rose'}


@dataclass(frozen=True)
class WebReference:
    prompt: str
    query: str
    official_mipt: bool = False
    official_requested: bool = False
    art_direction: bool = False
    auto_trace: bool = False
    alternative: bool = False


_DRAW = re.compile(r'^\s*(?:нарисуй|изобрази)\b', re.I)
_PEN_STATUS_BEFORE_DRAW = re.compile(
    r'^\s*(?:перо|ручка|подъ[её]м\s+пера)\s+(?:(?:уже|теперь|сейчас)\s+)?'
    r'(?:поднимается|работает|срабатывает|настроен[ао]?)\s*[,.;!]\s*'
    r'(?=(?:нарисуй|изобрази)\b)', re.I)
_PHOTO_OVERLAY = re.compile(
    r'\b(?:на|поверх)\s+(?:фото(?:графи\w*)?|сним(?:ке|ка)|изображени[еяи])\b'
    r'(?:\s+([^,.!?;]+?)(?=\s+и\s+|[,.;!?]|$))?', re.I)
_FIND = r'(?:найди|поищи|отыщи|загугли|погугли|ищи)'
_FOLLOWUP = re.compile(r'^\s*(?:ну\s+)?' + _FIND +
                       r'(?:\s+(?:его|е[ёе]|это|картинку|логотип))?(?:\s+сам(?:остоятельно)?)?\s*[.!?]*\s*$', re.I)
_MIPT = re.compile(r'\bлого(?:тип)?\s+(?:самого\s+)?(?:МФТИ|MIPT)\b', re.I)
_WEB = re.compile(r'\b(?:по\s+(?:образцу|картинке|фото)|референс\w*|'
                  r'(?:с|на)\s+(?:официального\s+|официальном\s+)?сайт\w*|'
                  r'в\s+интернете|(?:за|по)гугл\w*)\b', re.I)
_NAMED_LOGO = re.compile(r'\bлого(?:тип)?\s+(?!для\b|в\b|с\b|на\b|под\b)[\w«"]+', re.I)
_CAR = re.compile(r'\b(?:машин[ауые]|автомобил\w*|спорткар\w*|car|porsche|порше|bmw|бмв|'
                  r'ferrari|феррари|mercedes|мерседес|audi|ауди|lamborghini|ламборгини|'
                  r'ford mustang|форд мустанг|nissan skyline|ниссан скайлайн)\b', re.I)
_GENERIC_CAR = re.compile(r'\b(?:машин[ауые]|автомобил\w*|спорткар\w*|car)\b', re.I)
_SELF_DRAW = re.compile(r'^\s*(?:нет[\s,.!]+|тогда\s+|лучше\s+)?(?:'
                       r'(?:нарисуй|изобрази)\s+(?:сам(?:остоятельно)?|без\s+(?:поиска|образца|фото))|'
                       r'(?:давай\s+)?без\s+(?:поиска|образца|фото))[\s.!?]*$', re.I)
_FIND_IMAGE = re.compile(r'^\s*' + _FIND + r'\s+'
                         r'(?:(?:мне|сам|самостоятельно|в\s+интернете|именно)\s+)*'
                         r'(?=(?:официальн\w*\s+)?(?:лого|фото|изображени|картинк|референс))', re.I)
_TRACE_CHAIN = re.compile(
    r'(?:,\s*|\s+и\s+|\s+(?:затем|потом)\s+)(?:(?:просто|затем|потом)\s+)?'
    r'(?:обведи|трассируй|векторизуй)(?:\s+(?:е[ёе]|его|это|картинку|фото))?'
    r'(?:\s+по\s+контур\w*)?(?:\s+и\s+(?:закинь|покажи|добавь|вставь)'
    r'(?:\s+(?:е[ёе]|его|это|результат))?\s+(?:сюда|сда|здесь|в\s+эскиз|в\s+предпросмотр))?[\s.!?]*$', re.I)
_RETRY_TRACE = re.compile(r'^\s*(?:сделай\s+(?:что|как)\s+сказано|сделай\s+как\s+я\s+просил[аи]?|'
                          r'повтори\s+поиск|это\s+не\s+то|возьми\s+другой\s+образец)[\s.!?]*$', re.I)
_VAZ_2107 = re.compile(r'(?<!\w)(?:(?:ВАЗ|VAZ|Лада|Lada)[ -]?)?2107(?!\w)(?:\s+\(Lada 2107\))?', re.I)


def drawing_clause(prompt):
    """Separate a confirmed pen status from the explicit drawing instruction."""
    return _PEN_STATUS_BEFORE_DRAW.sub('', prompt, count=1)


def web_reference_request(prompt, previous=None):
    """Find references for vehicle drawings, named logos and explicit source requests."""
    if previous and _RETRY_TRACE.fullmatch(prompt):
        previous_request = web_reference_request(previous)
        if previous_request is not None and previous_request.auto_trace:
            return replace(previous_request, alternative=True)
    followup = bool(_FOLLOWUP.fullmatch(prompt))
    if followup:
        if previous is None:
            return WebReference(prompt, '')
        prompt = previous
    drawing = _DRAW.match(prompt)
    finding = _FIND_IMAGE.match(prompt)
    find_action = finding or re.match(r'^\s*' + _FIND + r'\s+', prompt, re.I)
    chain = _TRACE_CHAIN.search(prompt) if find_action else None
    if chain is not None:
        subject = re.sub(r'^\s*(?:картинк\w*|фото(?:графи\w*)?|изображени\w*|референс\w*)\s+', '',
                         prompt[find_action.end():chain.start()], flags=re.I)
        query = request_query('нарисуй ' + _WEB.sub(' ', subject))
        if query:
            mipt = bool(_MIPT.search(prompt))
            return WebReference(prompt, 'официальный логотип МФТИ' if mipt else _source_query(query),
                                official_mipt=mipt,
                                official_requested=mipt or bool(re.search(r'официальн\w*|\bс\s+сайт\w*', subject, re.I)),
                                auto_trace=True)
    if not (drawing or finding):
        return WebReference(prompt, '') if followup else None
    if re.search(r'\b(?:напиши|напечатай|дорисуй|затем|потом)\b', prompt, re.I):
        return None
    if _MIPT.search(prompt):
        return WebReference(prompt, 'официальный логотип МФТИ', True, True)
    official = bool(re.search(r'\bофициальн\w*\b|\b(?:у\s+\w+\s+на|с)\s+сайт\w*', prompt, re.I))
    named_logo = bool(_NAMED_LOGO.search(prompt))
    art = bool(drawing and not official and not named_logo and _CAR.search(prompt) and
               (_CAR.search(_GENERIC_CAR.sub(' ', prompt)) or _WEB.search(prompt) or followup) and
               not re.search(r'\b(?:вымышленн\w*|без\s+образца|из\s+головы|по\s+памяти)\b', prompt, re.I))
    if not (finding or _WEB.search(prompt) or followup or (official and named_logo) or art):
        return None
    prepared = 'нарисуй ' + prompt[(finding or drawing).end():] if finding else prompt
    prepared = re.split(r',?\s+(?:котор\w*\s+есть\b|и\s+(?:возьми\s+)?(?:самый\s+)?перв\w*\b)',
                        prepared, maxsplit=1, flags=re.I)[0]
    backdrop = ''
    if art:
        prepared = re.sub(r'\b(?:красив\w*|лаконично|стильно|реалистично)\b', ' ', prepared, flags=re.I)
        parts = re.split(r'\s+на\s+фоне\s+', prepared, maxsplit=1, flags=re.I)
        prepared = parts[0]
        if len(parts) == 2:
            backdrop = parts[1]
    query = request_query(_WEB.sub(' ', prepared))
    if query and backdrop:
        query = (query + ' на фоне ' + _WEB.sub(' ', backdrop).strip(' ,.!'))[:180]
    if query is None and followup:
        return WebReference(prompt, '')
    return WebReference(prompt, query, official_requested=official, art_direction=art) if query else None


def self_draw_request(prompt, previous):
    """None is an unrelated turn; an empty string requests a subject."""
    if not _SELF_DRAW.fullmatch(prompt):
        return None
    from .creative_planner import eligible
    if not previous:
        return ''
    previous = drawing_clause(previous)
    if not _DRAW.match(previous):
        reference = drawing_reference_request(previous)
        if reference is None:
            return ''
        return f'Нарисуй {reference.query} без образца'
    if not eligible(previous):
        return ''
    subject = _WEB.sub(' ', previous).strip(' .!?')
    return f'{subject[:1750]} без образца' if eligible(subject) else ''


def drawing_reference_request(prompt, previous=None):
    if re.search(r'\b(?:без\s+(?:поиска|образца|фото)|из\s+головы|по\s+памяти)\b', prompt, re.I):
        return None
    if previous and _RETRY_TRACE.fullmatch(prompt):
        prior_request = drawing_reference_request(previous)
        if prior_request is not None:
            return replace(prior_request, alternative=True)
    clause = drawing_clause(prompt)
    previous_clause = drawing_clause(previous) if previous else None
    request = web_reference_request(clause, previous_clause)
    if request is not None and request.query and (request.auto_trace or _DRAW.match(request.prompt)):
        return replace(request, prompt=prompt) if clause != prompt else request
    if not _DRAW.match(clause):
        return None
    photo = _PHOTO_OVERLAY.search(clause)
    if photo is not None:
        subject = (photo.group(1) or '').strip()
        query = request_query('нарисуй ' + subject) if subject else None
        if previous_clause:
            prior = web_reference_request(previous_clause)
            previous_query = prior.query if prior else request_query(previous_clause)
            if previous_query and (not query or re.search(r'(?<!\w)' + re.escape(query) + r'(?!\w)',
                                                         previous_query, re.I)):
                query = previous_query
        if query:
            return WebReference(prompt, _source_query(query))
    return None


def _source_query(query):
    return _VAZ_2107.sub('ВАЗ-2107 (Lada 2107)', query, count=1)[:180]


def request_query(prompt):
    """Extract a short reference subject from an explicit drawing request."""
    from .creative_planner import eligible
    from .project_answers import editor_words_only, is_project_topic
    if not eligible(prompt) or not re.match(r'\s*(?:нарисуй|изобрази)\b', prompt, re.I):
        return None
    if re.search(r'\b(?:напиши|напечатай|добавь|дорисуй|затем|потом)\b', prompt, re.I):
        return None  # Do not silently replace a compound task with one picture.
    if is_project_topic(prompt) and not editor_words_only(prompt):
        return None
    text = re.sub(r'^\s*(?:нарисуй|изобрази)\s+', '', prompt, flags=re.I).strip()
    explicit = bool(re.search(r'по\s+(?:образцу|картинке|фото)|референс', text, re.I))
    geometric = bool(re.search(r'\b(?:круг\w*|эллипс\w*|треугольник\w*|трапец\w*|квадрат\w*|'
                              r'прямоугольник\w*|линейк\w*|рамк\w*|орнамент\w*|текст\w*|'
                              r'букв\w*|слово|надпись)\b', text, re.I))
    if geometric and not explicit:
        return None
    text = re.sub(r'\b(?:красиво|детализированн\w*|подробно|по\s+(?:образцу|картинке|фото))\b', '', text, flags=re.I)
    # Remove only recognized placement fields, not the rest of the subject.
    from .planner import _CORNERS
    placement = (r'\b(?:в\s+(?:середине|центре)(?:\s+листа)?|по\s+центру(?:\s+листа)?|'
                 r'(?:в|на)\s+(?:' + '|'.join(_CORNERS) + r')\s+углу(?:\s+листа)?)\b')
    text = re.sub(placement, ' ', text, flags=re.I)
    text = re.sub(r'\bширин(?:ой|а)\s+\d+(?:[.,]\d+)?\s*мм\b', ' ', text, flags=re.I)
    for original, replacement in _ALIASES.items():
        text = re.sub(r'\b' + re.escape(original) + r'\b', replacement, text, flags=re.I)
    text = re.sub(r'\b(?:вид\s+)?с\s*боку\b', 'side view', text, flags=re.I)
    text = re.sub(r'вид\s+спереди|спереди', 'front view', text, flags=re.I)
    text = re.sub(r'\s+и\s*$', '', text)
    if re.search(r'\s+и\s+', text, re.I):
        return None
    return re.sub(r'\s+', ' ', text).strip(' ,.!')[:180] or None
