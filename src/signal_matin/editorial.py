"""Sélection éditoriale du profil personnel, pilotée par config.yaml."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import unicodedata
import urllib.error
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .config import setting
from .connectors.tavily import SearchHit, TavilyError, search as tavily_search
from .models import FeatureArticle, NewsItem, SourceRef
from .source_material import Material, article_material, wikipedia_material
from .synthesis import compose_feature, llm_configured

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIERS = ("dossier", "article", "lecture")
DEFAULT_TARGETS = {"dossier": (700, 900), "article": (450, 600), "lecture": (300, 450)}
logger = logging.getLogger(__name__)
EVERGREEN_CATEGORIES = {"Philosophie", "Littérature", "Histoire", "Culture", "Musique"}


def local_date(config: dict, now: dt.datetime) -> dt.date:
    zone = ZoneInfo(str(setting(config, "editorial.timezone", "Australia/Sydney")))
    return now.astimezone(zone).date()


def categories_for_date(config: dict, date: dt.date) -> list[str]:
    rotation = setting(config, "editorial.rotation", {}) or {}
    if set(rotation) != set(WEEKDAYS):
        raise ValueError("editorial.rotation doit définir les sept jours de la semaine")
    for day, categories in rotation.items():
        if not isinstance(categories, list) or not 1 <= len(categories) <= 3:
            raise ValueError(f"Rotation invalide pour {day} : une à trois rubriques requises")
        if any(not isinstance(value, str) or not value.strip() for value in categories):
            raise ValueError(f"Rubrique vide pour {day}")
        if len(set(categories)) != len(categories):
            raise ValueError(f"Rubrique répétée pour {day}")
    return rotation[WEEKDAYS[date.weekday()]]


def targets(config: dict, tier: str) -> tuple[int, int]:
    values = setting(config, f"editorial.targets.{tier}", DEFAULT_TARGETS[tier])
    if not isinstance(values, list | tuple) or len(values) != 2:
        raise ValueError(f"Cible éditoriale invalide : {tier}")
    lower, upper = map(int, values)
    if not 100 <= lower <= upper <= 1200:
        raise ValueError(f"Cible éditoriale invalide : {tier}")
    return lower, upper


STOPWORDS = {
    "avec", "apres", "avant", "dans", "dont", "elle", "elles", "entre", "etre", "leurs",
    "pour", "plus", "sans", "sont", "sous", "sur", "cette", "celui", "nouveau", "nouvelle",
    "annonce", "rapport", "etude", "article", "source", "news", "from", "that", "this",
    "with", "about", "after", "before", "their", "more", "will", "into", "over", "study",
    "report", "new", "says", "the", "and", "les", "des", "une", "the", "est", "aux",
    "mission", "projet", "systeme", "technology", "research", "scientists", "researchers",
}


def _tokens(value: str) -> set[str]:
    folded = "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                     if not unicodedata.combining(char))
    return {word for word in re.findall(r"[a-z0-9]+", folded)
            if (len(word) >= 3 or word in {"ai", "ia"}) and word not in STOPWORDS}


def _entities(value: str) -> set[str]:
    names = re.findall(r"\b(?:[A-ZÀ-ÖØ-Þ][\wÀ-ÿ-]*)(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÿ-]*)*", value)
    return {" ".join(sorted(_tokens(name))) for name in names if _tokens(name)}


def _doi(item: NewsItem) -> str | None:
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+",
                      f"{item.source.url or ''} {item.title} {item.summary}", re.I)
    return match.group(0).rstrip(".,;)").casefold() if match else None


def _versioned_names(item: NewsItem) -> dict[str, str]:
    return {name.casefold(): version.casefold() for name, version in re.findall(
        r"\b([A-Z][A-Za-z-]+)\s+([IVX]{1,6}|\d{1,4})\b", item.title)}


def _event_dates(item: NewsItem) -> set[str]:
    return set(re.findall(r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b",
                          item.title + " " + item.summary[:450]))


def _canonical_url(item: NewsItem) -> str:
    parts = urlsplit(str(item.source.url or ""))
    query = "&".join(f"{key}={value}" for key, value in parse_qsl(parts.query)
                     if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"})
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                       parts.path.rstrip("/"), query, ""))


def _related(first: NewsItem, other: NewsItem) -> bool:
    if _canonical_url(first) == _canonical_url(other):
        return False
    dates = first.source.published_at, other.source.published_at
    if all(dates) and abs((dates[0] - dates[1]).total_seconds()) > 72 * 3600:
        return False
    versions_left, versions_right = _versioned_names(first), _versioned_names(other)
    if any(versions_left[name] != versions_right[name] for name in versions_left.keys() & versions_right.keys()):
        return False
    event_dates_left, event_dates_right = _event_dates(first), _event_dates(other)
    if event_dates_left and event_dates_right and not event_dates_left & event_dates_right:
        return False
    first_doi, other_doi = _doi(first), _doi(other)
    if first_doi and first_doi == other_doi:
        return True
    left, right = _tokens(first.title), _tokens(other.title)
    common = left & right
    if left and right and len(common) >= 3 and len(common) / min(len(left), len(right)) >= 0.7:
        return True
    names = _entities(first.title) & _entities(other.title)
    if not names:
        return False
    entity_words = set().union(*(_tokens(name) for name in names))
    event_words = common - entity_words
    context_left = _tokens(first.title + " " + first.summary[:450]) - entity_words
    context_right = _tokens(other.title + " " + other.summary[:450]) - entity_words
    return bool(event_words and len(context_left & context_right) >= 2)


def _evergreen(config: dict, category: str, date: dt.date) -> Material | None:
    topics = setting(config, f"editorial.evergreen.{category}", []) or []
    if not topics:
        return None
    for offset in range(len(topics)):
        topic = topics[(date.toordinal() + offset) % len(topics)]
        material = wikipedia_material(str(topic))
        if material:
            return material
    return None


def fallback_categories(config: dict, primary: list[str]) -> list[str]:
    """Ordre de repli explicite, limité aux rubriques ayant au moins un flux."""
    configured = setting(config, "editorial.fallback", []) or []
    available = {feed.get("category") for feed in setting(config, "news.feeds", []) or []
                 if isinstance(feed, dict)}
    return [category for category in dict.fromkeys(configured)
            if category in available and category not in primary]


@dataclass
class TavilyBudget:
    limit: int
    used: int = 0

    def reserve(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _document_chars(materials: list[Material]) -> int:
    return sum(len(material.text.strip()) for material in materials)


def _publisher_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").removeprefix("www.").casefold()
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in {
        "co.uk", "ac.uk", "gov.uk", "com.au", "org.au", "gov.au", "co.jp",
    }:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _independent_sources(materials: list[Material]) -> int:
    return len({_publisher_key(str(material.source.url or "")) for material in materials
                if material.source.url})


def _rich_enough(materials: list[Material], tier: str, config: dict) -> bool:
    minimum = int(setting(config, "tavily.rich_chars_dossier", 3000) or 3000) if tier == "dossier" else 1800
    return _document_chars(materials) >= max(900, minimum) and _independent_sources(materials) >= 2


def _discovery_ready(item: NewsItem, config: dict) -> bool:
    minimum = max(40, int(setting(config, "tavily.min_discovery_chars", 120) or 120))
    return len(item.expanded_summary or item.summary) >= minimum and len(_tokens(item.title)) >= 3


def _search_query(item: NewsItem) -> str:
    title = item.title.strip()
    title_tokens = _tokens(title)
    names = [name for name in sorted(_entities(item.summary[:450]), key=len, reverse=True)
             if len(name) >= 4 and not _tokens(name).issubset(title_tokens)]
    concepts = sorted(_tokens(item.summary[:450]) - title_tokens, key=lambda word: (-len(word), word))[:3]
    bits = [title, *names[:2], *concepts]
    doi = _doi(item)
    if doi:
        bits.append(doi)
    if item.source.published_at:
        bits.append(str(item.source.published_at.year))
    return " ".join(bits)[:240]


def _same_story(first: NewsItem, hit: NewsItem) -> bool:
    """Recoupement d'événement sans confondre deux annonces d'une même entité."""
    if _doi(first) and _doi(first) == _doi(hit):
        return True
    versions_left, versions_right = _versioned_names(first), _versioned_names(hit)
    if any(versions_left[name] != versions_right[name] for name in versions_left.keys() & versions_right.keys()):
        return False
    dates_left, dates_right = _event_dates(first), _event_dates(hit)
    if dates_left and dates_right and not dates_left & dates_right:
        return False
    shared = _tokens(first.title) & _tokens(hit.title + " " + hit.summary[:500])
    title_shared = _tokens(first.title) & _tokens(hit.title)
    entity_words = set().union(*(_tokens(name) for name in _entities(first.title) & _entities(hit.title)))
    return len(shared) >= 3 and len(title_shared) >= 2 and bool(shared - entity_words)


def _near_duplicate(left: str, right: str) -> bool:
    def shingles(text: str) -> set[tuple[str, ...]]:
        words = re.findall(r"\w+", text.casefold())[:2000]
        return {tuple(words[i:i + 5]) for i in range(max(0, len(words) - 4))}
    a, b = shingles(left), shingles(right)
    return bool(a and b and len(a & b) / min(len(a), len(b)) >= 0.80)


def _source_priority(hit: SearchHit) -> tuple[int, float]:
    domain = hit.publisher.casefold()
    if domain.endswith((".gov", ".edu", ".ac.uk")) or "arxiv.org" in domain:
        priority = 0
    elif any(name in domain for name in ("nasa.gov", "europa.eu", "unesco.org", "who.int")):
        priority = 0
    elif any(name in domain for name in ("reuters.com", "apnews.com", "bbc.com", "euronews.com", "nature.com", "github.blog", "cloudflare.com")):
        priority = 1
    else:
        priority = 2
    return priority, -hit.score


def _tavily_materials(first: NewsItem, current: list[Material], config: dict,
                      budget: TavilyBudget, cache: dict[str, Material], tier: str) -> list[Material]:
    if not bool(setting(config, "tavily.enabled", False)):
        return current
    if not os.environ.get("TAVILY_API_KEY", "").strip():
        logger.info("[Tavily] clé absente ; sources existantes conservées")
        return current
    if not _discovery_ready(first, config):
        logger.info("[%s] sujet trop vague pour une recherche Tavily", first.category)
        return current
    per_article = max(0, min(5, int(setting(config, "tavily.max_searches_per_article", 2) or 0)))
    max_results = max(1, min(8, int(setting(config, "tavily.max_results_per_search", 8) or 8)))
    query = _search_query(first)
    for attempt in range(per_article):
        if _rich_enough(current, tier, config):
            break
        if not budget.reserve():
            logger.info("[Tavily] budget épuisé : %d/%d recherches", budget.used, budget.limit)
            break
        logger.info("[Tavily] recherche %d/%d", budget.used, budget.limit)
        try:
            hits = tavily_search(query if attempt == 0 else first.title,
                                 max_results=max_results,
                                 recent=first.category not in EVERGREEN_CATEGORIES)
        except TavilyError as error:
            logger.warning("[Tavily] recherche impossible (%s)", error)
            break
        logger.info("[Tavily] %d résultats", len(hits))
        retained = 0
        for hit in sorted(hits, key=_source_priority):
            if len(current) >= 5:
                break
            domain = hit.publisher.casefold()
            if any(bad in domain for bad in ("pinterest.", "quora.", "reddit.", "medium.com")):
                continue
            if any(marker in (hit.title + " " + hit.content).casefold() for marker in
                   ("client challenge", "subscribe to continue", "enable javascript to continue")):
                continue
            if sum(_publisher_key(str(material.source.url or "")) ==
                   _publisher_key(hit.url) for material in current) >= 2:
                continue
            if first.source.published_at and hit.published_at and (
                hit.published_at < first.source.published_at - dt.timedelta(days=14)
            ) and first.category not in EVERGREEN_CATEGORIES:
                continue
            item = NewsItem(title=hit.title, category=first.category,
                            summary=hit.content[:1600], expanded_summary=hit.content,
                            source=SourceRef(name=hit.publisher, title=hit.title, url=hit.url,
                                             published_at=hit.published_at))
            if not _same_story(first, item):
                continue
            key = _canonical_url(item)
            if key in {_canonical_url(first.model_copy(update={"source": material.source}))
                       for material in current}:
                continue
            if key not in cache:
                fetched = article_material(item)
                cache[key] = (fetched if fetched.origin == "page" else
                              Material(fetched.source, fetched.title, fetched.text,
                                       fetched.license_note, "tavily"))
            material = cache[key]
            if len(material.text.strip()) < 100 or any(_near_duplicate(material.text, old.text)
                                                       for old in current):
                continue
            current.append(material)
            retained += 1
        logger.info("[Tavily] %d résultats retenus", retained)
    return current


def write_features(
    config: dict, date: dt.date, selected: list[str], items: list[NewsItem],
    *, load_category: Callable[[str], list[NewsItem]] | None = None,
    primary: list[str] | None = None,
) -> list[FeatureArticle]:
    if not bool(setting(config, "synthesis.enabled", False)) or not llm_configured():
        logger.warning("Rédaction IA désactivée ou paramètres IA incomplets ; aucun appel IA")
        return []
    features: list[FeatureArticle] = []
    used_urls: set[str] = set()
    material_cache: dict[str, Material] = {}
    budget = TavilyBudget(max(0, int(setting(config, "tavily.max_searches_per_edition", 6) or 0)))
    primary = primary if primary is not None else selected
    for category in selected:
        if len(features) >= 3:
            break
        tier = TIERS[len(features)]
        if load_category and category not in primary:
            items.extend(load_category(category))
        candidates = [item for item in items if item.category == category
                      and _canonical_url(item) not in used_urls]
        materials: list[Material] = []
        chosen: NewsItem | None = None
        if candidates:
            keywords = [str(word).casefold() for word in
                        (setting(config, "interests.music_keywords", []) or [])]
            def score(item: NewsItem) -> float:
                corroboration = len({other.source.name for other in candidates
                                     if other is not item and _related(item, other)})
                interest = (category == "Musique" and any(
                    word in f"{item.title} {item.summary}".casefold() for word in keywords
                ))
                return 2 * corroboration + 2 * interest + min(len(item.expanded_summary or item.summary), 4000) / 1000

            for first in sorted(candidates, key=score, reverse=True):
                if any(_same_story(item, first) or _related(item, first)
                       for item in items if _canonical_url(item) in used_urls):
                    continue
                related = [item for item in candidates if item is not first and _related(first, item)]
                related.sort(key=lambda item: (
                    item.source.name != first.source.name,
                    len(item.expanded_summary or item.summary)), reverse=True)
                group: list[Material] = []
                group_urls: set[str] = set()
                for item in [first, *related]:
                    key = _canonical_url(item)
                    if key in group_urls:
                        continue
                    group_urls.add(key)
                    if key not in material_cache:
                        material_cache[key] = article_material(item)
                    group.append(material_cache[key])
                    if len(group) >= 3:
                        break
                logger.info("[%s] sujet détecté: %.100s", category, first.title)
                logger.info("[%s] matière initiale: %d caractères / %d source(s)",
                            category, _document_chars(group), len(group))
                if not _rich_enough(group, tier, config) and _discovery_ready(first, config):
                    logger.info("[%s] documentation à enrichir -> recherche Tavily si configurée", category)
                    group = _tavily_materials(first, group, config, budget, material_cache, tier)
                    logger.info("[%s] dossier enrichi: %d caractères / %d source(s)",
                                category, _document_chars(group), len(group))
                if _document_chars(group) >= 900:
                    materials = group
                    chosen = first
                    break
                if _document_chars(group) > _document_chars(materials):
                    materials, chosen = group, first
        if _document_chars(materials) < 900:
            evergreen = _evergreen(config, category, date)
            if evergreen:
                materials = [evergreen]
        if _document_chars(materials) < 900:
            logger.warning("%s : %d source(s), %d caractères exploitables ; minimum 900, aucun appel IA",
                           category, len(materials), _document_chars(materials))
            continue
        try:
            target = targets(config, tier)
            logger.info("[%s] validation documentaire OK", category)
            logger.info("[%s] appel GPT-4.1-mini avec %d source(s), %d caractères",
                        category, len(materials), _document_chars(materials))
            feature = compose_feature(category, tier, materials, target)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            if isinstance(error, urllib.error.HTTPError):
                logger.error("%s : appel IA refusé (HTTP %d, request_id=%s)",
                             category, error.code, error.headers.get("x-request-id", "absent"))
            else:
                logger.error("%s : rédaction IA impossible (%s)", category, type(error).__name__)
            feature = None
        if feature:
            if _independent_sources(materials) < 2 or (tier == "dossier" and
                    _document_chars(materials) < int(setting(config, "tavily.rich_chars_dossier", 3000) or 3000)):
                feature = feature.model_copy(update={"shortfall": True})
            features.append(feature)
            logger.info("[%s] article validé", category)
            cited_urls = {str(source.url) for source in feature.sources if source.url}
            used_urls.update(_canonical_url(item) for item in candidates
                             if str(item.source.url) in cited_urls)
            if chosen:
                used_urls.add(_canonical_url(chosen))
                used_urls.update(_canonical_url(item) for item in candidates if _related(chosen, item))
    return features
