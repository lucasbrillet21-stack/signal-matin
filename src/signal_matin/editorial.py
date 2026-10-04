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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .config import setting
from .connectors.tavily import (SearchHit, SearchTrace, TavilyError, capture_search,
                                search as tavily_search)
from .models import ApiCost, FeatureArticle, NewsItem, RubricDiagnostic, SourceRef
from .source_material import Material, article_material, wikipedia_material
from .synthesis import ComposeTrace, capture_compose, compose_feature, llm_configured

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIERS = ("dossier", "article", "lecture")
DEFAULT_TARGETS = {"dossier": (700, 900), "article": (450, 600), "lecture": (300, 450)}
logger = logging.getLogger(__name__)
EVERGREEN_CATEGORIES = {"Philosophie", "Littérature", "Histoire", "Culture", "Musique",
                        "Mythologies & Religions"}


def _safe_log(value: object, limit: int = 240) -> str:
    result = " ".join(str(value or "").split())
    for key in ("TAVILY_API_KEY", "SIGNAL_MATIN_LLM_API_KEY", "GMAIL_APP_PASSWORD"):
        secret = os.environ.get(key, "")
        if secret:
            result = result.replace(secret, "[SECRET MASQUÉ]")
    return result[:limit]


def _safe_log_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = urlencode([(key, value) for key, value in parse_qsl(parts.query)
                           if not any(word in key.casefold() for word in
                                      ("token", "secret", "password", "api_key", "auth"))])
        return _safe_log(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")), 500)
    except ValueError:
        return "[URL invalide]"


def _log_search_result(domain: str, title: str, url: str, chars: int,
                       decision: str, reason: str = "") -> None:
    logger.info("[Tavily] résultat | domaine=%s | titre=%s | URL=%s | contenu=%d caractères | "
                "décision=%s | raison=%s", _safe_log(domain, 100), _safe_log(title),
                _safe_log_url(url), chars, decision, reason or "—")


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


# Vocabulaire d'événements bilingue : on rapproche les concepts, pas seulement les
# graphies identiques. Le filtre ci-dessous garde aussi un acteur et un objet communs.
_CONCEPTS = {
    "strike": {"frappe", "frappes", "frapper", "strike", "strikes", "attack", "attacks", "attacked", "attaque", "attaques", "bombardement", "bombardements"},
    "refinery": {"raffinerie", "raffineries", "refinery", "refineries", "refining"},
    "russia": {"russie", "russe", "russes", "russian", "russians"},
    "ukraine": {"ukraine", "ukrainien", "ukrainienne", "ukrainiennes", "ukrainiens", "ukrainian", "ukrainians"},
    "greenland": {"groenland", "greenland", "groenlandais", "greenlandic"},
    "mineral": {"minerai", "minerais", "minerals", "mineral", "ressources", "resources", "rare", "terres"},
    "graphite": {"graphite"},
    "mining": {"mine", "mines", "mining", "extraction", "extractive"},
    "intensify": {"intensifie", "intensification", "intensified", "intensifies", "ramp", "ramps", "increase", "increased"},
    "sanction": {"sanction", "sanctions", "embargo"},
    "election": {"election", "elections", "élection", "élections", "electoral", "scrutin"},
    "diplomacy": {"diplomatie", "diplomatique", "diplomatic", "diplomacy", "summit", "sommet", "negotiation", "négociation"},
    "government": {"gouvernement", "government", "ministre", "minister", "president", "président", "parliament", "parlement"},
    "security": {"securite", "sécurité", "security", "defense", "défense", "military", "militaire"},
    "energy": {"energie", "énergie", "energy", "oil", "petrole", "pétrole", "gaz", "gas"},
}
_CONCEPT_BY_WORD = {word: concept for concept, words in _CONCEPTS.items() for word in words}
_EVENT_ACTIONS = {"strike", "intensify", "sanction", "election", "diplomacy", "government", "security", "mining"}
_EVENT_OBJECTS = {"refinery", "graphite", "mineral", "energy", "election", "sanction"}


def _concept_tokens(value: str) -> set[str]:
    return {_CONCEPT_BY_WORD.get(token, token) for token in _tokens(value)}


def _same_story_semantic(first: NewsItem, hit: NewsItem) -> bool:
    """Recoupement V2 : mots exacts ou acteur, action et objet bilingues concordants."""
    distinctive = {"refinery", "graphite", "mineral", "election", "sanction"}
    required_objects = _concept_tokens(first.title) & distinctive
    if required_objects and not required_objects & _concept_tokens(
            hit.title + " " + (hit.expanded_summary or hit.summary)[:1000]):
        return False
    if _same_story(first, hit):
        return True
    versions_left, versions_right = _versioned_names(first), _versioned_names(hit)
    if any(versions_left[name] != versions_right[name] for name in versions_left.keys() & versions_right.keys()):
        return False
    dates_left, dates_right = _event_dates(first), _event_dates(hit)
    if dates_left and dates_right and not dates_left & dates_right:
        return False
    left = _concept_tokens(first.title)
    right = _concept_tokens(hit.title)
    shared = left & right
    if len(shared) < 3:
        return False
    if not (shared - _EVENT_ACTIONS - _EVENT_OBJECTS):
        return False
    if not (shared & (_EVENT_ACTIONS | _EVENT_OBJECTS)):
        return False
    left_objects, right_objects = left & _EVENT_OBJECTS, right & _EVENT_OBJECTS
    if left_objects and right_objects and not left_objects & right_objects:
        return False
    left_actions, right_actions = left & _EVENT_ACTIONS, right & _EVENT_ACTIONS
    if left_actions and right_actions and not left_actions & right_actions:
        return False
    return len(_concept_tokens(first.title + " " + first.summary[:500]) &
               _concept_tokens(hit.title + " " + hit.summary[:500])) >= 3


def _international_priority(item: NewsItem) -> float | None:
    """Exige un signal géopolitique, sans liste de personnalités à exclure."""
    title = _concept_tokens(item.title)
    body = _concept_tokens(item.title + " " + (item.expanded_summary or item.summary)[:900])
    geopolitics = {"strike", "sanction", "election", "diplomacy", "government", "security",
                   "war", "guerre", "conflict", "conflit", "treaty", "traite", "frontiere", "border"}
    culture = {"book", "livre", "edition", "publishing", "publisher", "culture", "exposition", "museum", "musée"}
    if not (body & geopolitics):
        return None
    if title & culture and not title & geopolitics:
        return None
    return 4 * len(title & geopolitics) + len(body & geopolitics)


def _near_duplicate(left: str, right: str) -> bool:
    def shingles(text: str) -> set[tuple[str, ...]]:
        words = re.findall(r"\w+", text.casefold())[:2000]
        return {tuple(words[i:i + 5]) for i in range(max(0, len(words) - 4))}
    a, b = shingles(left), shingles(right)
    return bool(a and b and len(a & b) / min(len(a), len(b)) >= 0.80)


def _source_priority(hit: SearchHit) -> tuple[int, float]:
    domain = hit.publisher.casefold()
    if domain.endswith((".gov", ".edu", ".ac.uk", ".museum")) or "arxiv.org" in domain:
        priority = 0
    elif any(name in domain for name in ("nasa.gov", "europa.eu", "unesco.org", "who.int")):
        priority = 0
    elif any(name in domain for name in ("reuters.com", "apnews.com", "bbc.com", "euronews.com", "nature.com", "github.blog", "cloudflare.com")):
        priority = 1
    else:
        priority = 2
    return priority, -hit.score


def filter_tavily_hits(first: NewsItem, current: list[Material], hits: list[SearchHit],
                       cache: dict[str, Material] | None = None,
                       on_decision: Callable[[SearchHit, str, str], None] | None = None,
                       relevant: Callable[[NewsItem, NewsItem], bool] | None = None,
                       max_sources: int = 5) -> list[Material]:
    """Applique le même filtrage de pertinence, de sources et de doublons que l'édition."""
    cache = cache if cache is not None else {}
    for hit in sorted(hits, key=_source_priority):
        def reject(reason: str) -> None:
            if on_decision:
                on_decision(hit, "REJETÉ", reason)

        if len(current) >= max_sources:
            reject("dossier_full")
            continue
        domain = hit.publisher.casefold()
        if first.category in EVERGREEN_CATEGORIES and any(
                bad in domain for bad in ("amazon.", "facebook.", "youtube.",
                                          "instagram.", "tiktok.")):
            reject("non_documentary_domain")
            continue
        if any(bad in domain for bad in ("pinterest.", "quora.", "reddit.", "medium.com")):
            reject("seo_domain")
            continue
        if any(marker in (hit.title + " " + hit.content).casefold() for marker in
               ("client challenge", "subscribe to continue", "enable javascript to continue")):
            reject("challenge_page")
            continue
        if sum(_publisher_key(str(material.source.url or "")) ==
               _publisher_key(hit.url) for material in current) >= 2:
            reject("domain_overrepresented")
            continue
        if first.source.published_at and hit.published_at and (
            hit.published_at < first.source.published_at - dt.timedelta(days=14)
        ) and first.category not in EVERGREEN_CATEGORIES:
            reject("too_old")
            continue
        item = NewsItem(title=hit.title, category=first.category,
                        summary=hit.content[:1600], expanded_summary=hit.content,
                        source=SourceRef(name=hit.publisher, title=hit.title, url=hit.url,
                                         published_at=hit.published_at))
        if not (relevant(first, item) if relevant else _same_story(first, item)):
            reject("irrelevant")
            continue
        key = _canonical_url(item)
        if key in {_canonical_url(first.model_copy(update={"source": material.source}))
                   for material in current}:
            reject("duplicate_url")
            continue
        if key not in cache:
            fetched = article_material(item)
            cache[key] = (fetched if fetched.origin == "page" else
                          Material(fetched.source, fetched.title, fetched.text,
                                   fetched.license_note, "tavily"))
        material = cache[key]
        if len(material.text.strip()) < (300 if first.category in EVERGREEN_CATEGORIES else 100):
            reject("content_too_short")
            continue
        if any(_near_duplicate(material.text, old.text) for old in current):
            reject("duplicate_content")
            continue
        current.append(material)
        if on_decision:
            on_decision(hit, "RETENU", "")
    return current


def _tavily_materials(first: NewsItem, current: list[Material], config: dict,
                      budget: TavilyBudget, cache: dict[str, Material], tier: str,
                      row: RubricDiagnostic | None = None, candidate_number: int = 0) -> list[Material]:
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
        if row:
            row.tavily_searches += 1
        actual_query = query if attempt == 0 else first.title
        logger.info("[Tavily]\nRubrique : %s\nCandidat : #%d — %s\nRecherche : %d/%d\n"
                    "Recherche pour ce candidat : %d/%d\nBudget restant : %d\nRequête : \"%s\"",
                    _safe_log(first.category, 80), candidate_number, _safe_log(first.title),
                    budget.used, budget.limit, attempt + 1, per_article,
                    budget.limit - budget.used, _safe_log(actual_query))
        trace = SearchTrace()
        try:
            with capture_search(trace):
                hits = tavily_search(actual_query, max_results=max_results,
                                     recent=first.category not in EVERGREEN_CATEGORIES)
        except TavilyError as error:
            logger.warning("[Tavily] recherche impossible (%s)", error)
            break
        if trace.credits is not None:
            if row:
                row.tavily_credits = (row.tavily_credits or 0) + trace.credits
            logger.info("[Tavily] crédits indiqués par l'API : %d", trace.credits)
        else:
            logger.info("[Tavily] crédits API non fournis ; recherches effectuées cette édition : %d",
                        budget.used)
        logger.info("[Tavily] résultats reçus : %d", trace.raw_count or len(hits))
        for rejected in trace.rejected:
            _log_search_result(rejected.domain, rejected.title, rejected.url,
                               rejected.content_chars, "REJETÉ", rejected.reason)
        before = len(current)
        filter_tavily_hits(first, current, hits, cache,
                           on_decision=lambda hit, decision, reason: _log_search_result(
                               hit.publisher, hit.title, hit.url, len(hit.content), decision, reason))
        logger.info("[Tavily] résultats retenus : %d", len(current) - before)
    return current


def _record_material(row: RubricDiagnostic, materials: list[Material]) -> None:
    row.material_chars = _document_chars(materials)
    row.source_count = len(materials)
    row.domain_count = _independent_sources(materials)


def _log_dossier(category: str, before: list[Material], after: list[Material],
                 tier: str, config: dict) -> None:
    logger.info("[%s] dossier enrichi\nMatière avant : %d caractères\n"
                "Matière après : %d caractères\nSources avant : %d\nSources après : %d\n"
                "Domaines indépendants : %d\nObjectif richesse atteint : %s\n"
                "Seuil rédaction 900 atteint : %s", _safe_log(category, 80),
                _document_chars(before), _document_chars(after), len(before), len(after),
                _independent_sources(after), "OUI" if _rich_enough(after, tier, config) else "NON",
                "OUI" if _document_chars(after) >= 900 else "NON")
    for index, material in enumerate(after, 1):
        logger.info("[%d] %s — %s", index,
                    _safe_log(_publisher_key(str(material.source.url or "")), 100),
                    _safe_log(material.title))


def _log_run_summary(rows: list[RubricDiagnostic], searches: int, limit: int,
                     articles: int) -> None:
    logger.info("Résumé du run | Rubrique | candidats essayés | recherches Tavily | "
                "matière finale | sources | domaines | LLM appelé | résultat")
    for row in rows:
        logger.info("Résumé du run | %s | %d | %d | %d | %d | %d | %s | %s%s",
                    _safe_log(row.category, 80), row.candidates_tried, row.tavily_searches,
                    row.material_chars, row.source_count, row.domain_count,
                    "OUI" if row.llm_called else "NON", row.result,
                    f" ({_safe_log(row.reason, 120)})" if row.reason else "")
    logger.info("Recherches Tavily totales : %d / %d", searches, limit)
    logger.info("Articles publiés : %d / 3", articles)
    known_credits = [row.tavily_credits for row in rows if row.tavily_credits is not None]
    if known_credits:
        logger.info("Crédits déclarés dans les réponses Tavily disponibles : %d", sum(known_credits))


def _safe_error_description(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401, 403):
            return "authentification ou autorisation refusée"
        if error.code == 429:
            return "limite de débit ou quota atteint"
        if error.code >= 500:
            return "erreur du serveur de rédaction"
        return "requête rejetée par l'API de rédaction"
    if isinstance(error, (OSError, TimeoutError)):
        return "connexion ou délai de rédaction indisponible"
    return "réponse de rédaction invalide"


def write_features(
    config: dict, date: dt.date, selected: list[str], items: list[NewsItem],
    *, load_category: Callable[[str], list[NewsItem]] | None = None,
    primary: list[str] | None = None,
    diagnostics: list[RubricDiagnostic] | None = None,
    costs: list[ApiCost] | None = None,
) -> list[FeatureArticle]:
    rows = diagnostics if diagnostics is not None else []
    if bool(setting(config, "editorial.v2.enabled", False)) and llm_configured():
        from .editorial_v2 import write_features_v2
        return write_features_v2(config, date, selected, items, load_category, primary, rows, costs)
    budget = TavilyBudget(max(0, int(setting(config, "tavily.max_searches_per_edition", 6) or 0)))
    if not bool(setting(config, "synthesis.enabled", False)) or not llm_configured():
        logger.warning("Rédaction IA désactivée ou paramètres IA incomplets ; aucun appel IA")
        rows.extend(RubricDiagnostic(category=category, result="writing_error", reason="api_non_configurée")
                    for category in (primary if primary is not None else selected))
        _log_run_summary(rows, 0, budget.limit, 0)
        return []
    features: list[FeatureArticle] = []
    used_urls: set[str] = set()
    material_cache: dict[str, Material] = {}
    primary = primary if primary is not None else selected
    for category in selected:
        if len(features) >= 3:
            break
        tier = TIERS[len(features)]
        row = RubricDiagnostic(category=category)
        rows.append(row)
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
                row.candidates_tried += 1
                logger.info("[%s] candidat #%d\nTitre : %s\nSource de découverte : %s\n"
                            "Date : %s\nMatière initiale : %d caractères\n"
                            "Nombre de sources initiales : %d\nNombre de domaines indépendants : %d",
                            _safe_log(category, 80), row.candidates_tried, _safe_log(first.title),
                            _safe_log(first.source.name, 120),
                            first.source.published_at.isoformat() if first.source.published_at else "absente",
                            _document_chars(group), len(group), _independent_sources(group))
                if not _rich_enough(group, tier, config) and _discovery_ready(first, config):
                    logger.info("[%s] documentation à enrichir -> recherche Tavily si configurée", category)
                    before = list(group)
                    group = _tavily_materials(first, group, config, budget, material_cache, tier,
                                              row, row.candidates_tried)
                    _log_dossier(category, before, group, tier, config)
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
        _record_material(row, materials)
        if _document_chars(materials) < 900:
            row.result = "tavily_insufficient" if row.tavily_searches else "documentation_insufficient"
            row.reason = ("moins_de_900_après_recherche" if row.tavily_searches else
                          "aucun_sujet" if not row.candidates_tried else "moins_de_900_sans_recherche")
            logger.warning("%s : %d source(s), %d caractères exploitables ; minimum 900, aucun appel IA",
                           category, len(materials), _document_chars(materials))
            continue
        trace = ComposeTrace()
        feature: FeatureArticle | None = None
        try:
            target = targets(config, tier)
            logger.info("[%s] validation documentaire OK", category)
            logger.info("[%s] appel LLM\nModèle : %s\nNombre de sources : %d\n"
                        "Nombre de domaines : %d\nCaractères documentaires : %d",
                        _safe_log(category, 80),
                        _safe_log(os.environ.get("SIGNAL_MATIN_LLM_MODEL", "non configuré"), 100),
                        len(materials), _independent_sources(materials), _document_chars(materials))
            row.llm_called = True
            with capture_compose(trace):
                feature = compose_feature(category, tier, materials, target)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            row.result = "article_rejected" if trace.validation == "REJET" else "writing_error"
            row.reason = trace.reason or type(error).__name__
            if isinstance(error, urllib.error.HTTPError):
                logger.error("%s : appel IA refusé (HTTP %d, request_id=%s ; type=%s ; message=%s)",
                             category, error.code,
                             _safe_log(error.headers.get("x-request-id", "absent"), 100),
                             type(error).__name__, _safe_error_description(error))
            else:
                logger.error("%s : rédaction IA impossible (type=%s ; message=%s)",
                             category, type(error).__name__, _safe_error_description(error))
        received = trace.response_received or feature is not None
        if feature:
            validation, reason = "OK", trace.reason or "—"
        else:
            validation, reason = "REJET", trace.reason or row.reason or "aucun_article_retourné"
            if row.result not in {"writing_error", "article_rejected"}:
                row.result = "article_rejected"
                row.reason = reason
        logger.info("[%s] réponse reçue : %s | longueur article : %d mots | "
                    "validation : %s | raison : %s", _safe_log(category, 80),
                    "OUI" if received else "NON", feature.word_count() if feature else trace.word_count,
                    validation, _safe_log(reason, 120))
        if feature:
            if _independent_sources(materials) < 2 or (tier == "dossier" and
                    _document_chars(materials) < int(setting(config, "tavily.rich_chars_dossier", 3000) or 3000)):
                reasons = list(dict.fromkeys([*feature.shortfall_reasons, "documentation_limited"]))
                feature = feature.model_copy(update={"shortfall": True, "shortfall_reasons": reasons})
            features.append(feature)
            row.result = "published"
            row.reason = ",".join(feature.shortfall_reasons)
            logger.info("[%s] article validé", category)
            cited_urls = {str(source.url) for source in feature.sources if source.url}
            used_urls.update(_canonical_url(item) for item in candidates
                             if str(item.source.url) in cited_urls)
            if chosen:
                used_urls.add(_canonical_url(chosen))
                used_urls.update(_canonical_url(item) for item in candidates if _related(chosen, item))
    _log_run_summary(rows, budget.used, budget.limit, len(features))
    return features
