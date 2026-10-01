"""Sélection éditoriale du profil personnel, pilotée par config.yaml."""
from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
import urllib.error
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .config import setting
from .models import FeatureArticle, NewsItem
from .source_material import Material, article_material, wikipedia_material
from .synthesis import compose_feature, llm_configured

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIERS = ("dossier", "article", "lecture")
DEFAULT_TARGETS = {"dossier": (700, 900), "article": (450, 600), "lecture": (300, 450)}
logger = logging.getLogger(__name__)


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


def write_features(
    config: dict, date: dt.date, selected: list[str], items: list[NewsItem],
) -> list[FeatureArticle]:
    if not bool(setting(config, "synthesis.enabled", False)) or not llm_configured():
        logger.warning("Rédaction IA désactivée ou paramètres IA incomplets ; aucun appel IA")
        return []
    features: list[FeatureArticle] = []
    used_urls: set[str] = set()
    material_cache: dict[str, Material] = {}
    for category in selected:
        if len(features) >= 3:
            break
        tier = TIERS[len(features)]
        candidates = [item for item in items if item.category == category
                      and _canonical_url(item) not in used_urls]
        materials: list[Material] = []
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
                if sum(len(material.text) for material in group) >= 900:
                    materials = group
                    break
                if sum(len(material.text) for material in group) > sum(len(material.text) for material in materials):
                    materials = group
        if sum(len(material.text) for material in materials) < 900:
            evergreen = _evergreen(config, category, date)
            if evergreen:
                materials = [evergreen]
        if sum(len(material.text) for material in materials) < 900:
            logger.warning("%s : %d source(s), %d caractères exploitables ; minimum 900, aucun appel IA",
                           category, len(materials), sum(len(material.text) for material in materials))
            continue
        try:
            target = targets(config, tier)
            logger.warning("%s : appel IA avec %d source(s), %d caractères",
                           category, len(materials), sum(len(material.text) for material in materials))
            feature = compose_feature(category, tier, materials, target)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            if isinstance(error, urllib.error.HTTPError):
                logger.error("%s : appel IA refusé (HTTP %d, request_id=%s)",
                             category, error.code, error.headers.get("x-request-id", "absent"))
            else:
                logger.error("%s : rédaction IA impossible (%s)", category, type(error).__name__)
            feature = None
        if feature:
            features.append(feature)
            cited_urls = {str(source.url) for source in feature.sources if source.url}
            used_urls.update(_canonical_url(item) for item in candidates
                             if str(item.source.url) in cited_urls)
    return features
