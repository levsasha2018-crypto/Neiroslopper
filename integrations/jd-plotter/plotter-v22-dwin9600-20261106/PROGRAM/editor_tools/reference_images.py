"""Opt-in, read-only Commons reference search. No scraping, keys or uploads."""
from dataclasses import dataclass, asdict
from html import unescape
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

API = 'https://commons.wikimedia.org/w/api.php'
MAX_DOWNLOAD = 8_000_000
USER_AGENT = 'PlotterReference/1.0 (desktop reference selection; no uploads)'


class ReferenceCancelled(ValueError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ReferenceCancelled('Операция отменена.')


def checked_url(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname not in
                ('commons.wikimedia.org', 'upload.wikimedia.org', 'thumb.wikimedia.org') or parts.username or
                parts.password or parts.port not in (None, 443) or parts.fragment):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Источник должен быть HTTPS-адресом Wikimedia Commons.') from None
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, *, limit=MAX_DOWNLOAD, cancel=None, timeout=12):
    check_cancel(cancel)
    checked_url(url)
    try:
        opener = build_opener(SafeRedirect())
        with opener.open(Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'application/json,image/*;q=0.9'}), timeout=timeout) as response:
            checked_url(response.geturl())
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
            raise ValueError('Wikimedia временно ограничила загрузки. Подождите перед повтором или откройте локальную картинку; автоматических повторов нет.') from exc
        raise ValueError('Источник недоступен. Выберите другой образец или откройте локальную картинку.') from exc
    except (OSError, TimeoutError) as exc:
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
