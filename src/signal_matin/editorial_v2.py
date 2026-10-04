"""Pipeline éditorial V2 : recherche, draft, critique, complément, réécriture."""
from __future__ import annotations

import datetime as dt
import logging
import os
import urllib.error
from urllib.parse import urlsplit

from .api_cost import UsageCounter, capture_usage, estimate
from .config import setting
from .connectors.tavily import SearchTrace, TavilyError, capture_search, search as tavily_search
from .models import ApiCost, FeatureArticle, NewsItem, RubricDiagnostic
from .source_material import Material, article_material
from .synthesis import ComposeTrace, EditorialCritique, capture_compose, compose_feature, critique_feature
from .timeless import TIMELESS, remember_published, topic_for_date
from .usage_ledger import MonthlyCreditLedger
from . import editorial as base

logger = logging.getLogger("signal_matin.editorial")


def _source_type(material: Material) -> str:
    domain = urlsplit(str(material.source.url or "")).hostname or ""
    if domain.endswith((".gov", ".edu", ".ac.uk", ".museum")) or any(
            name in domain for name in ("unesco.org", "nasa.gov", "europa.eu")):
        return "institution"
    if "arxiv.org" in domain or "doi.org" in domain:
        return "academic"
    if domain:
        return "publisher"
    return "unknown"


def _coverage(materials: list[Material]) -> set[str]:
    text = " ".join(material.text[:4000] for material in materials).casefold()
    signals = {
        "contexte": ("history", "histor", "contexte", "background", "origine", "previous"),
        "chiffres": ("percent", "%", "million", "chiffre", "data", "statistic"),
        "mécanisme": ("how", "comment", "mechanism", "mécanisme", "method", "technique"),
        "conséquence": ("impact", "conséquence", "effect", "résultat", "outcome"),
    }
    return {name for name, words in signals.items() if any(word in text for word in words)}


def _research_complete(materials: list[Material], tier: str, config: dict,
                       searches: int, target: int) -> bool:
    if not base._rich_enough(materials, tier, config):
        return False
    types = {_source_type(material) for material in materials}
    if searches >= target:
        return len(types) >= 2 or base._independent_sources(materials) >= 3
    # Dossier exceptionnel : trois domaines, deux types, plusieurs angles et matière abondante.
    return (base._independent_sources(materials) >= 3 and len(types) >= 2 and
            len(_coverage(materials)) >= 3 and base._document_chars(materials) >= 6000)


def _angles(first: NewsItem) -> list[tuple[str, str]]:
    title = first.title.strip()
    historical = first.category in TIMELESS
    anchor = "archives musée université source primaire" if historical else "source officielle primaire"
    return [
        (base._search_query(first), "événement principal"),
        (f"{title} contexte chronologie chiffres", "contexte et chiffres"),
        (f"{title} causes mécanisme origine", "mécanisme ou origine"),
        (f"{title} {anchor}", "source primaire"),
        (f"{title} analyse indépendante limites", "analyse indépendante"),
        (f"{title} conséquences impact", "conséquences"),
        (f"{title} données études documentation", "données et études"),
        (f"{title} contradictions débats", "contradictions et nuances"),
        (f"{title} archives sources académiques", "archives et recherche"),
        (f"{title} comparaison historique perspective", "perspective comparative"),
    ]


def _timeless_relevant(first: NewsItem, other: NewsItem) -> bool:
    """Rapproche un dossier intemporel sans exiger trois mots d'un titre d'actualité."""
    anchor = base._tokens(first.title)
    title = base._tokens(other.title)
    body = base._tokens(other.title + " " + (other.expanded_summary or other.summary)[:1000])
    common = anchor & body
    if not common or not (anchor & title):
        return False
    if len(anchor) >= 3:
        return len(common) >= 2
    if len(common) >= 2:
        return True
    if first.category == "Mythologies & Religions":
        return bool(body & {"myth", "mythology", "mythe", "religion", "religious",
                            "bible", "biblical", "coran", "quran", "scripture"})
    return False


class ResearchBudget:
    def __init__(self, config: dict, date: dt.date):
        self.limit = max(0, int(setting(config, "tavily.max_searches_per_edition", 30)))
        self.used = 0
        self.credits = 0
        self.ledger = MonthlyCreditLedger(date.strftime("%Y-%m"),
                                         int(setting(config, "tavily.monthly_credit_budget", 1500)))
        logger.info("[Research] crédits locaux connus ce mois : %d/%d ; "
                    "ce compteur ne représente pas le solde Tavily réel",
                    self.ledger.used(), self.ledger.limit)

    def reserve(self) -> bool:
        if self.used >= self.limit or not self.ledger.reserve():
            return False
        self.used += 1
        self.credits += 1
        return True

    def reconcile(self, api_credits: int | None) -> None:
        if api_credits is not None and api_credits > 1:
            # Le connecteur utilise basic ; toute différence signalée est conservée.
            self.credits += api_credits - 1
            self.ledger.add_reported_extra(api_credits - 1)


def _research(first: NewsItem, materials: list[Material], config: dict,
              budget: ResearchBudget, row: RubricDiagnostic, cache: dict[str, Material],
              tier: str, phase: str, queries: list[tuple[str, str]], maximum: int,
              target: int = 0, seen_queries: set[str] | None = None) -> None:
    if not bool(setting(config, "tavily.enabled", False)) or not os.environ.get("TAVILY_API_KEY", "").strip():
        return
    seen_queries = seen_queries if seen_queries is not None else set()
    attempts = 0
    stagnant = 0
    for query, reason in queries:
        if attempts >= maximum:
            break
        if phase == "pre-draft" and _research_complete(materials, tier, config, attempts, target):
            break
        normalized = " ".join(query.casefold().split())
        if normalized in seen_queries:
            continue
        if not budget.reserve():
            logger.info("[Research] budget édition ou mensuel atteint ; arrêt")
            break
        seen_queries.add(normalized)
        attempts += 1
        row.tavily_searches += 1
        logger.info("[Research] article: %s | phase: %s | search: %d/%d | query: %s | reason: %s",
                    base._safe_log(first.title), phase, attempts, maximum,
                    base._safe_log(query), base._safe_log(reason))
        trace = SearchTrace()
        try:
            with capture_search(trace):
                hits = tavily_search(query, max_results=int(setting(config, "tavily.max_results_per_search", 8)),
                                     recent=first.category not in base.EVERGREEN_CATEGORIES)
        except TavilyError as error:
            logger.warning("[Research] Tavily erreur : %s", base._safe_log(error))
            break
        budget.reconcile(trace.credits)
        if trace.credits is not None:
            row.tavily_credits = (row.tavily_credits or 0) + trace.credits
        logger.info("[Research] reçus=%d | avant=%d caractères | domaines=%d | types=%s | couverture=%s",
                    trace.raw_count or len(hits), base._document_chars(materials),
                    base._independent_sources(materials),
                    ",".join(sorted({_source_type(m) for m in materials})),
                    ",".join(sorted(_coverage(materials))))
        for rejected in trace.rejected:
            base._log_search_result(rejected.domain, rejected.title, rejected.url,
                                    rejected.content_chars, "REJETÉ", rejected.reason)
        previous = (base._document_chars(materials), base._independent_sources(materials),
                    len(_coverage(materials)))
        base.filter_tavily_hits(first, materials, hits, cache,
                                on_decision=lambda hit, decision, why: base._log_search_result(
                                    hit.publisher, hit.title, hit.url, len(hit.content), decision, why),
                                relevant=_timeless_relevant if first.category in TIMELESS else None)
        current = (base._document_chars(materials), base._independent_sources(materials),
                   len(_coverage(materials)))
        stagnant = stagnant + 1 if current == previous else 0
        logger.info("[Research] après=%d caractères | domaines=%d | types=%s | couverture=%s",
                    base._document_chars(materials), base._independent_sources(materials),
                    ",".join(sorted({_source_type(m) for m in materials})),
                    ",".join(sorted(_coverage(materials))))
        if phase == "pre-draft" and attempts >= target and stagnant >= 2 and base._rich_enough(materials, tier, config):
            logger.info("[Research] arrêt anticipé : dossier riche, deux recherches sans apport")
            break


def write_features_v2(config: dict, date: dt.date, selected: list[str], items: list[NewsItem],
                      load_category, primary: list[str] | None,
                      rows: list[RubricDiagnostic], costs: list[ApiCost] | None) -> list[FeatureArticle]:
    budget = ResearchBudget(config, date)
    counter = UsageCounter()
    llm_attempts = 0
    features: list[FeatureArticle] = []
    cache: dict[str, Material] = {}
    primary = primary if primary is not None else selected
    used_urls: set[str] = set()
    with capture_usage(counter):
        for category in selected:
            if len(features) >= 3:
                break
            row = RubricDiagnostic(category=category)
            rows.append(row)
            if load_category and category not in primary and category not in TIMELESS:
                items.extend(load_category(category))
            if category in TIMELESS:
                topic = topic_for_date(config, category, date)
                candidates = [topic] if topic else []
            else:
                candidates = [item for item in items if item.category == category and
                              base._canonical_url(item) not in used_urls]
                keywords = [str(word).casefold() for word in
                            (setting(config, "interests.music_keywords", []) or [])]
                def score(item: NewsItem) -> float:
                    corroboration = len({other.source.name for other in candidates
                                         if other is not item and base._related(item, other)})
                    interest = (category == "Musique" and any(
                        word in f"{item.title} {item.summary}".casefold() for word in keywords))
                    return (2 * corroboration + 2 * interest +
                            min(len(item.expanded_summary or item.summary), 4000) / 1000)
                candidates.sort(key=score, reverse=True)
            for first in candidates:
                if not first:
                    continue
                row.candidates_tried += 1
                if category in TIMELESS:
                    materials: list[Material] = []
                else:
                    related = [item for item in candidates if item is not first and base._related(first, item)][:2]
                    group = [first, *related]
                    materials = []
                    for item in group:
                        key = base._canonical_url(item)
                        if key not in cache:
                            cache[key] = article_material(item)
                        if all(str(existing.source.url) != str(cache[key].source.url) for existing in materials):
                            materials.append(cache[key])
                logger.info("[%s] candidat #%d | titre=%s | découverte=%s | date=%s | "
                            "matière=%d | sources=%d | domaines=%d", category, row.candidates_tried,
                            base._safe_log(first.title), base._safe_log(first.source.name),
                            first.source.published_at.isoformat() if first.source.published_at else "intemporel",
                            base._document_chars(materials), len(materials), base._independent_sources(materials))
                seen_queries: set[str] = set()
                if not base._rich_enough(materials, base.TIERS[len(features)], config):
                    _research(first, materials, config, budget, row, cache, base.TIERS[len(features)],
                              "pre-draft", _angles(first),
                              min(10, int(setting(config, "tavily.max_pre_draft_searches", 10))),
                              int(setting(config, "tavily.pre_draft_target_searches", 3)), seen_queries)
                base._record_material(row, materials)
                if row.material_chars < 900:
                    row.result = "tavily_insufficient" if row.tavily_searches else "documentation_insufficient"
                    row.reason = "moins_de_900_caractères"
                    continue
                tier = base.TIERS[len(features)]
                active_trace: ComposeTrace | None = None
                try:
                    target = base.targets(config, tier)
                    row.llm_called = True
                    llm_attempts += 1
                    logger.info("[%s] appel LLM draft | modèle=%s | sources=%d | domaines=%d | "
                                "caractères=%d", category,
                                base._safe_log(os.environ.get("SIGNAL_MATIN_LLM_MODEL", ""), 100),
                                len(materials), base._independent_sources(materials),
                                base._document_chars(materials))
                    draft_trace = ComposeTrace()
                    active_trace = draft_trace
                    with capture_compose(draft_trace):
                        draft = compose_feature(category, tier, materials, target)
                    if draft is None:
                        row.result, row.reason = "article_rejected", draft_trace.reason or "draft_invalide"
                        continue
                    llm_attempts += 1
                    critique = critique_feature(draft, materials)
                    logger.info("[Critic] repetition: %.2f | continuity: %.2f | depth: %.2f | "
                                "additional research: %s", critique.repetition, critique.continuity,
                                critique.depth, "YES" if critique.additional_research_needed else "NO")
                    if critique.additional_research_needed:
                        gap = "; ".join([*critique.missing_context, *critique.missing_questions])[:180]
                        targeted = [(query, gap or "lacune signalée par le critique") for query in
                                    critique.suggested_search_queries if query.strip()]
                        logger.info("[Research] phase: post-critique | critic gap addressed: %s",
                                    base._safe_log(gap or "lacune documentaire", 180))
                        _research(first, materials, config, budget, row, cache, tier,
                                  "post-critique", targeted,
                                  min(5, int(setting(config, "tavily.max_post_critique_searches", 5))),
                                  seen_queries=seen_queries)
                    base._record_material(row, materials)
                    logger.info("[Rewrite] input sources: %d", len(materials))
                    llm_attempts += 1
                    final_trace = ComposeTrace()
                    active_trace = final_trace
                    with capture_compose(final_trace):
                        final = compose_feature(category, tier, materials, target,
                                                revision=(draft, critique))
                    if final is None:
                        row.result, row.reason = "article_rejected", final_trace.reason or "rewrite_invalide"
                        logger.info("[Rewrite] final words: %d | validation: REJET | raison=%s",
                                    final_trace.word_count, row.reason)
                        continue
                    if base._independent_sources(materials) < 2 or not base._rich_enough(materials, tier, config):
                        reasons = list(dict.fromkeys([*final.shortfall_reasons, "documentation_limited"]))
                        final = final.model_copy(update={"shortfall": True, "shortfall_reasons": reasons})
                    logger.info("[Rewrite] final words: %d | validation: OK", final.word_count())
                    if category in TIMELESS:
                        remember_published(category, first.title, date)
                    else:
                        used_urls.add(base._canonical_url(first))
                    features.append(final)
                    row.result, row.reason = "published", ",".join(final.shortfall_reasons)
                    break
                except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
                    row.result = ("article_rejected" if active_trace and
                                  active_trace.validation == "REJET" else "writing_error")
                    row.reason = active_trace.reason if active_trace and active_trace.reason else type(error).__name__
                    status = error.code if isinstance(error, urllib.error.HTTPError) else "—"
                    logger.error("[%s] erreur rédaction/API : HTTP %s | type=%s | message=%s",
                                 category, status, type(error).__name__, base._safe_error_description(error))
    if counter.llm_calls != llm_attempts:
        counter.complete = False
        counter.llm_calls = llm_attempts
    result = estimate(config, counter, budget.used, budget.credits)
    if costs is not None:
        costs.append(result)
    base._log_run_summary(rows, budget.used, budget.limit, len(features))
    return features
