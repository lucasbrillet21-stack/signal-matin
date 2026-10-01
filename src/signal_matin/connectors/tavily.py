"""Recherche documentaire Tavily : extraits sourcés, sans réponse rédigée."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from email.utils import parsedate_to_datetime
from typing import Iterator
from urllib.parse import urlsplit

ENDPOINT = "https://api.tavily.com/search"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    content: str
    publisher: str
    published_at: dt.datetime | None = None
    score: float = 0.0


class TavilyError(Exception):
    """Erreur de recherche sûre à afficher : aucun corps ni en-tête secret."""


@dataclass(frozen=True)
class SearchRejection:
    title: str
    url: str
    domain: str
    content_chars: int
    reason: str


@dataclass
class SearchTrace:
    raw_count: int = 0
    credits: int | None = None
    rejected: list[SearchRejection] = field(default_factory=list)


_trace: ContextVar[SearchTrace | None] = ContextVar("tavily_search_trace", default=None)


@contextmanager
def capture_search(trace: SearchTrace) -> Iterator[None]:
    token = _trace.set(trace)
    try:
        yield
    finally:
        _trace.reset(token)


def _public_https(url: str) -> tuple[bool, str]:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if parts.scheme != "https" or not host or parts.username or parts.password:
            return False, ""
        if parts.port not in (None, 443) or host == "localhost" or host.endswith((".local", ".internal")):
            return False, ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                return False, ""
        return True, host.removeprefix("www.")
    except ValueError:
        return False, ""


def _date(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def search(query: str, *, max_results: int = 8, recent: bool = True) -> list[SearchHit]:
    """Interroge Search. `content` est un extrait, jamais une réponse Tavily."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise TavilyError("clé TAVILY_API_KEY absente")
    if not query.strip():
        return []
    payload = {
        "query": query.strip()[:240],
        "search_depth": "basic",
        "chunks_per_source": 3,
        "max_results": max(1, min(8, int(max_results))),
        "topic": "news" if recent else "general",
        "include_published_date": True,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "include_usage": True,
        "safe_search": True,
    }
    if recent:
        payload["time_range"] = "week"
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        raise TavilyError(f"HTTP {error.code}") from None
    except (OSError, ValueError) as error:
        raise TavilyError(type(error).__name__) from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise TavilyError("réponse invalide")
    trace = _trace.get()
    if trace is not None:
        trace.raw_count = len(data["results"])
        usage = data.get("usage")
        credits = usage.get("credits") if isinstance(usage, dict) else None
        if isinstance(credits, int) and not isinstance(credits, bool) and credits >= 0:
            trace.credits = credits
    hits: list[SearchHit] = []
    for entry in data["results"][:payload["max_results"]]:
        if not isinstance(entry, dict):
            if trace is not None:
                trace.rejected.append(SearchRejection("", "", "", 0, "invalid_result"))
            continue
        url = str(entry.get("url") or "")
        title = str(entry.get("title") or "").strip()[:240]
        content = str(entry.get("content") or "").strip()[:12_000]
        allowed, publisher = _public_https(url)
        if not allowed:
            if trace is not None:
                trace.rejected.append(SearchRejection(title, url, "", len(content), "invalid_url"))
            continue
        if not title or not content:
            if trace is not None:
                trace.rejected.append(SearchRejection(title, url, publisher, len(content),
                                                      "missing_title" if not title else "missing_content"))
            continue
        try:
            score = float(entry.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        hits.append(SearchHit(title, url, content, publisher,
                              _date(entry.get("published_date")), score))
    return hits
