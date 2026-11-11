"""Fetch public HTTPS images for a user-requested reference search."""
import http.client
from html.parser import HTMLParser
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit

from .reference_images import MAX_DOWNLOAD, USER_AGENT, check_cancel


IMAGE_TYPES = frozenset(('image/svg+xml', 'image/png', 'image/jpeg', 'image/webp', 'image/gif'))
_DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}\Z', re.I)


def public_url(value):
    if (not isinstance(value, str) or not 12 <= len(value) <= 2000 or
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Нужен публичный HTTPS-адрес изображения.')
    parts = urlsplit(value)
    try:
        host = (parts.hostname or '').encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise ValueError('Некорректное имя сайта.') from exc
    if (parts.scheme != 'https' or not _DOMAIN.fullmatch(host) or parts.username is not None or
            parts.password is not None or parts.port not in (None, 443) or
            host.endswith(('.localhost', '.local', '.internal', '.onion')) or
            '\\' in value):
        raise ValueError('Нужен публичный HTTPS-адрес изображения.')
    return parts._replace(netloc=host, fragment='')


def global_address(value):
    ip = ipaddress.ip_address(value)
    if ip.version == 6:
        if ip.sixtofour or ip.teredo:
            return False
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Pin the validated numeric address for this actual connection.
        # TLS still verifies the hostname from the public URL.
        records = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        candidates = [entry for entry in records if global_address(entry[4][0])]
        if not candidates or len(candidates) != len(records):
            raise ValueError('Ссылка ведёт на непубличный сетевой адрес.')
        last_error = None
        deadline = time.monotonic() + self.timeout
        for family, kind, protocol, _, address in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wire = socket.socket(family, kind, protocol)
            try:
                wire.settimeout(remaining)
                wire.connect(address)
                self.sock = self._context.wrap_socket(wire, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
                wire.close()
        raise OSError('Публичный сайт не ответил.') from last_error


def _download(url, accepted, limit, cancel):
    deadline = time.monotonic() + 20
    try:
        for _ in range(4):
            check_cancel(cancel)
            parts = public_url(url)
            timeout = min(7, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError()
            connection = PublicHTTPS(parts.hostname, 443, timeout=timeout, context=ssl.create_default_context())
            try:
                target = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
                if parts.query:
                    target += '?' + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~[]")
                connection.request('GET', target, headers={'User-Agent': USER_AGENT,
                                                           'Accept': 'text/html,image/*' if 'text/html' in accepted else 'image/*',
                                                           'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise ValueError('Сайт вернул пустое перенаправление.')
                    url = urljoin(url, location)
                    continue
                mime = (response.getheader('Content-Type') or '').split(';', 1)[0].strip().casefold()
                if response.status != 200 or mime not in accepted:
                    raise ValueError('Ссылка не вернула поддерживаемый файл.')
                data = bytearray()
                while len(data) <= limit:
                    check_cancel(cancel)
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    block = response.read1(min(32768, limit + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                if not data or len(data) > limit:
                    raise ValueError('Изображение пустое или слишком большое.')
                return bytes(data), mime, url
            finally:
                connection.close()
        raise ValueError('Сайт вернул слишком много перенаправлений.')
    except (OSError, http.client.HTTPException, UnicodeError) as exc:
        raise ValueError('Не удалось загрузить изображение с найденного сайта.') from exc


def download_image(url, *, cancel=None, limit=MAX_DOWNLOAD):
    body, mime, _ = _download(url, IMAGE_TYPES, limit, cancel)
    return body, mime


class _ImageLinks(HTMLParser):
    def __init__(self, page, logo):
        super().__init__()
        self.page = page
        self.logo = logo
        self.results = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'img':
            href = attrs.get('src')
            description = ' '.join(attrs.get(key) or '' for key in ('alt', 'title', 'class'))
        elif tag == 'a':
            href = attrs.get('href')
            description = attrs.get('title') or ''
            if not href or not re.search(r'\.(?:svg|png|jpe?g|webp)(?:\?|$)', href, re.I):
                return
        elif tag == 'meta' and attrs.get('property') == 'og:image':
            href = attrs.get('content')
            description = ''
        else:
            return
        if not href or len(self.results) >= 30:
            return
        try:
            url = urljoin(self.page, href)
            parts = public_url(url)
        except ValueError:
            return
        logo_match = bool(re.search(r'logo|логотип', parts.path + ' ' + description, re.I))
        if self.logo and not logo_match:
            return
        if not self.logo and tag != 'meta' and not description:
            return
        rank = (not parts.path.casefold().endswith('.svg'), tag == 'meta') if self.logo else (tag != 'meta', False)
        self.results.setdefault(url, rank)


def image_links(page, subject, *, cancel=None):
    """Find image links in HTML without running any site code."""
    body, mime, final_url = _download(page, IMAGE_TYPES | {'text/html'}, 800_000, cancel)
    if mime in IMAGE_TYPES:
        return [final_url]
    links = _ImageLinks(final_url, bool(re.search(r'logo|лого', subject, re.I)))
    links.feed(body.decode('utf-8', errors='replace'))
    return sorted(links.results, key=links.results.get)[:2]
