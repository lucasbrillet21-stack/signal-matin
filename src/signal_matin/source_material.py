"""Matériaux de lecture bornés pour la rédaction, jamais imprimés tels quels."""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

from .models import NewsItem, SourceRef

USER_AGENT = "Signal-Matin/1.1 (https://github.com/sosoj92/signal-matin)"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Material:
    source: SourceRef
    title: str
    text: str
    license_note: str = ""


def _get(url: str, *, limit: int = 1_000_000) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=12) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return response.read(limit).decode(encoding, errors="replace")


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.paragraph_depth = 0
        self.buffer: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag == "article":
            self.depth += 1
        elif self.depth and tag == "p":
            self.paragraph_depth += 1
            if self.paragraph_depth == 1:
                self.buffer = []

    def handle_endtag(self, tag: str):
        if tag == "p" and self.paragraph_depth:
            self.paragraph_depth -= 1
            if self.paragraph_depth == 0:
                value = " ".join(html.unescape("".join(self.buffer)).split())
                if len(value) >= 55:
                    self.paragraphs.append(value)
        elif tag == "article" and self.depth:
            self.depth -= 1

    def handle_data(self, data: str):
        if self.depth and self.paragraph_depth:
            self.buffer.append(data)


def _robots_allow(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    parser = RobotFileParser()
    parser.parse(_get(robots_url, limit=200_000).splitlines())
    return parser.can_fetch(USER_AGENT, url)


def article_material(item: NewsItem) -> Material:
    """N'utilise le corps public que si robots.txt l'autorise ; sinon le RSS suffit."""
    text = item.expanded_summary or item.summary
    url = str(item.source.url or "")
    if url.startswith("https://"):
        try:
            if _robots_allow(url):
                parser = _ArticleParser()
                page = _get(url)
                parser.feed(page)
                body = "\n".join(parser.paragraphs)
                if len(body) >= 400:
                    text = body[:12_000]
                elif re.search(r"<title[^>]*>\s*Client Challenge\s*</title>", page, re.I):
                    logger.warning("%s : page de vérification reçue à la place de l'article", item.source.name)
                else:
                    logger.warning("%s : corps d'article trop court (%d caractères)", item.source.name, len(body))
            else:
                logger.warning("%s : lecture de l'article interdite par robots.txt", item.source.name)
        except (OSError, ValueError, UnicodeError) as error:
            logger.warning("%s : lecture de l'article impossible (%s, HTTP %s)",
                           item.source.name, type(error).__name__, getattr(error, "code", "—"))
    return Material(source=item.source, title=item.title, text=text)


def wikipedia_material(topic: str) -> Material | None:
    """Extrait Wikipédia français, avec attribution et lien de licence dans le rendu."""
    query = urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "explaintext": "1",
        "redirects": "1", "format": "json", "titles": topic, "maxlag": "5",
    })
    try:
        data = json.loads(_get(f"https://fr.wikipedia.org/w/api.php?{query}"))
        page = next(iter(data["query"]["pages"].values()))
        if "missing" in page:
            return None
        text = re.sub(r"\n{3,}", "\n\n", page.get("extract", "")).strip()
        if len(text) < 700:
            return None
        title = page["title"]
        url = "https://fr.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        return Material(
            source=SourceRef(name="Wikipédia, contributeurs", url=url),
            title=title, text=text[:16_000],
            license_note="Source Wikipédia, CC BY-SA 4.0 ; rédaction adaptée.",
        )
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return None
