"""Collecte RSS/Atom generique, sans dependance a un fournisseur."""
from __future__ import annotations

import datetime as dt
import html
import logging
import math
import os
import re
import urllib.request
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..models import DataSourceStatus, DataState, NewsItem, SourceRef

logger = logging.getLogger(__name__)


class FeedPayload(bytes):
    def __new__(cls, value: bytes, http_status: int | None):
        result = super().__new__(cls, value)
        result.http_status = http_status
        return result


def _safe_log(value: str) -> str:
    result = " ".join(value.split())
    for key in ("TAVILY_API_KEY", "SIGNAL_MATIN_LLM_API_KEY", "GMAIL_APP_PASSWORD"):
        secret = os.environ.get(key, "")
        if secret:
            result = result.replace(secret, "[SECRET MASQUÉ]")
    return result[:240]


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _text(element: ET.Element, names: set[str]) -> str:
    for child in element:
        if _tag(child) in names:
            value = "".join(child.itertext())
            if value:
                return " ".join(value.split())
    return ""


def _link(element: ET.Element) -> str | None:
    for child in element:
        if _tag(child) == "link" and child.attrib.get("rel", "alternate") == "alternate":
            return child.attrib.get("href") or (child.text or "").strip() or None
    return None


def _plain(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(value or "")).split())


def feed_texts(element: ET.Element) -> dict[str, str]:
    """Return usable RSS/Atom text fields, preserving the richest field separately."""
    result: dict[str, str] = {}
    for child in element:
        tag = _tag(child)
        if tag in {"description", "summary", "encoded", "content"}:
            value = _plain(" ".join(child.itertext()))
            if value:
                result[tag] = value
    return result


def _matches_any(value: str, keywords: list[str]) -> bool:
    return not keywords or any(re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)",
                                    value.casefold()) for word in keywords if word.strip())


def _date(element: ET.Element) -> dt.datetime | None:
    value = _text(element, {"pubdate", "published", "updated", "date"})
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def _payload(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Signal-Matin/1.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return FeedPayload(response.read(2_000_000), getattr(response, "status", None))


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query)
                       if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def collect_rss(
    feeds: list, now: dt.datetime, *, limit: int = 12, max_age_hours: int = 72,
    status_name: str = "Actualites", require_date: bool = False,
) -> tuple[list[NewsItem], DataSourceStatus]:
    if not feeds:
        return [], DataSourceStatus(
            name=status_name, state=DataState.DISABLED,
            detail="Aucun flux RSS configure.",
        )
    buckets: list[list[NewsItem]] = []
    seen: set[str] = set()
    cutoff = now - dt.timedelta(hours=max_age_hours)
    future_limit = now + dt.timedelta(hours=6)
    per_feed = max(2, math.ceil(limit / max(1, len(feeds))))
    errors = 0
    for entry in feeds:
        if isinstance(entry, str):
            name, category, url = "Source", "Actualites", entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("nom") or "Source")
            category = str(entry.get("category") or entry.get("categorie") or "Actualites")
            url = str(entry.get("url") or "")
            include_any = [str(word) for word in (entry.get("include_any") or [])]
        else:
            continue
        if isinstance(entry, str):
            include_any = []
        if not url:
            continue
        try:
            payload = _payload(url)
            try:
                root = ET.fromstring(payload)
            except ET.ParseError:
                repaired = payload.decode("utf-8", errors="replace")
                repaired = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", repaired)
                repaired = re.sub(
                    r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]+;)",
                    "&amp;", repaired,
                )
                root = ET.fromstring(repaired)
            bucket: list[NewsItem] = []
            nodes = [node for node in root.iter() if _tag(node) in {"item", "entry"}]
            within_window = sum(1 for node in nodes if
                                (published := _date(node)) is not None and
                                cutoff <= published <= future_limit)
            logger.info("[RSS %s / %s] HTTP %s | entrées reçues : %d | fenêtre temporelle : %d",
                        _safe_log(name), _safe_log(category),
                        getattr(payload, "http_status", "inconnu"), len(nodes), within_window)
            for node in nodes:
                title = _plain(_text(node, {"title"}))
                link = _link(node)
                if not link or urlsplit(link).scheme not in {"http", "https"}:
                    continue
                key = _canonical(link).casefold()
                published = _date(node)
                title_key = re.sub(r"\W+", " ", title.casefold()).strip()
                if not title or key in seen or title_key in seen:
                    continue
                if require_date and published is None:
                    continue
                if published and (published < cutoff or published > future_limit):
                    continue
                texts = feed_texts(node)
                summary = texts.get("description") or texts.get("summary") or texts.get("encoded") or texts.get("content") or ""
                rich_text = max(texts.values(), key=len, default=summary)
                if not _matches_any(f"{title} {summary[:500]}", include_any):
                    continue
                seen.add(key)
                seen.add(title_key)
                bucket.append(NewsItem(
                    title=title,
                    category=category,
                    summary=(summary[:1600] or "Resume non fourni par le flux."),
                    expanded_summary=rich_text[:12_000],
                    source=SourceRef(name=name, title=title, url=link, published_at=published),
                ))
                age = (now - published).total_seconds() / 3600 if published else None
                logger.info("[RSS %s / %s] candidat retenu | titre=%s | date=%s | âge=%s h | "
                            "contenu RSS=%d caractères",
                            _safe_log(name), _safe_log(category), _safe_log(title),
                            published.isoformat() if published else "absente",
                            f"{age:.1f}" if age is not None else "inconnu",
                            len(rich_text[:12_000]))
                if len(bucket) >= per_feed:
                    break
            if bucket:
                buckets.append(bucket)
        except Exception as error:
            logger.warning("%s : flux RSS impossible (%s, HTTP %s)",
                           name, type(error).__name__, getattr(error, "code", "—"))
            logger.info("[RSS %s / %s] HTTP %s | entrées reçues : 0 | fenêtre temporelle : 0 | échec=%s",
                        _safe_log(name), _safe_log(category), getattr(error, "code", "inconnu"),
                        type(error).__name__)
            errors += 1
    items: list[NewsItem] = []
    while len(items) < limit and any(buckets):
        for bucket in buckets:
            if bucket and len(items) < limit:
                items.append(bucket.pop(0))
    state = DataState.LIVE if items else DataState.UNAVAILABLE
    detail = f"{len(feeds) - errors}/{len(feeds)} flux lus"
    return items, DataSourceStatus(
        name=status_name, state=state, detail=detail, item_count=len(items),
    )
