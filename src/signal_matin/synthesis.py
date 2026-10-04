"""Rédaction d'articles longs depuis des matériaux sourcés via API chat compatible."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from urllib.parse import urlsplit
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from pydantic import BaseModel, Field
from pydantic import ValidationError

from .api_cost import record_llm_usage
from .models import ArticleParagraph, FeatureArticle
from .source_material import Material

logger = logging.getLogger(__name__)


@dataclass
class ComposeTrace:
    phase: str = "draft"
    response_received: bool = False
    word_count: int = 0
    validation: str = "non démarrée"
    reason: str = ""


_trace: ContextVar[ComposeTrace | None] = ContextVar("compose_trace", default=None)


@contextmanager
def capture_compose(trace: ComposeTrace) -> Iterator[None]:
    token = _trace.set(trace)
    try:
        yield
    finally:
        _trace.reset(token)


def _mark(*, received: bool | None = None, words: int | None = None,
          validation: str | None = None, reason: str | None = None) -> None:
    trace = _trace.get()
    if trace is None:
        return
    if received is not None:
        trace.response_received = received
    if words is not None:
        trace.word_count = words
    if validation is not None:
        trace.validation = validation
    if reason is not None:
        trace.reason = reason
    if validation == "REJET":
        logger.warning("[Validation] phase=%s reason=%s", trace.phase, reason or "unknown")


def llm_configured() -> bool:
    return all(os.environ.get(key, "").strip() for key in (
        "SIGNAL_MATIN_LLM_URL", "SIGNAL_MATIN_LLM_API_KEY", "SIGNAL_MATIN_LLM_MODEL"
    ))


def _chat(messages: list[dict[str, str]], *, response_format: dict | None = None) -> str:
    endpoint = os.environ["SIGNAL_MATIN_LLM_URL"].strip()
    if not endpoint.startswith("https://"):
        raise ValueError("SIGNAL_MATIN_LLM_URL doit utiliser HTTPS")
    body = {
        "model": os.environ["SIGNAL_MATIN_LLM_MODEL"].strip(),
        "temperature": 0,
        "messages": messages,
    }
    if response_format:
        body["response_format"] = response_format
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Authorization": f"Bearer {os.environ['SIGNAL_MATIN_LLM_API_KEY'].strip()}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        _mark(received=True)
        data = json.load(response)
        record_llm_usage(data.get("usage"))
        return data["choices"][0]["message"]["content"].strip()


LONG_SYSTEM = """Tu es rédacteur d'un journal personnel en français. Écris un article de fond original.
Utilise uniquement les matériaux numérotés fournis. N'invente jamais de fait, date,
chiffre, citation, relation causale ou source. Ne reprends jamais plusieurs phrases
consécutives d'une source. N'accède à aucune connaissance non présente dans les
matériaux pour combler un manque. Ne cherche pas sur Internet. Si la matière est insuffisante, écris plus court
ou réponds exactement INSUFFISANT. Distingue les faits rapportés, le contexte,
les mécanismes, les conséquences, l'analyse et les limites. Sur les sujets
politiques, reste neutre et présente les perspectives présentes dans les sources
sans attribuer d'intentions. Chaque paragraphe doit citer au moins un numéro de
source existant. Ne produis aucune citation directe non fournie.
Ne présente jamais l'affirmation d'une seule source comme un consensus.
Signale les incertitudes et différencie clairement constat, contexte et analyse.
Écris un récit explicatif approfondi, pas une succession de résumés de sources.
Organise le texte en trois grandes parties cohérentes. Donne un heading au premier
paragraphe de chaque partie et un heading vide aux paragraphes suivants. Il faut
trois titres de partie distincts, pas un titre par dimension éditoriale.
Couvre contexte, faits,
mécanismes, analyse, conséquences et limites sans répéter labels ni informations.
Ne reviens pas artificiellement aux faits déjà exposés.
Réponds uniquement en JSON : {"title":"...","paragraphs":[{"heading":"titre de partie ou vide pour la suite","text":"...","dimensions":["facts","context"],"source_ids":["S1"]}]}.
Les six dimensions facts, context, mechanisms, analysis, consequences, limits doivent
être couvertes dans l'ensemble de l'article, sans imposer six parties. Un paragraphe
peut couvrir plusieurs dimensions. Utilise exactement les identifiants S1, S2, etc.
qui figurent dans le dossier ; chaque affirmation factuelle doit être appuyée.
Écris des paragraphes de 50 à 140 mots ; conserve des transitions naturelles.
La conclusion doit exposer au moins une limite ou incertitude documentée.
"""


def compose_feature(
    category: str, tier: str, materials: list[Material], target: tuple[int, int],
    revision: tuple[FeatureArticle, "EditorialCritique"] | None = None,
    v2: bool = False,
) -> FeatureArticle | None:
    """Rédige depuis des matériaux réels ; toute sortie est validée avant publication."""
    if not llm_configured() or not materials:
        _mark(validation="REJET", reason="api_non_configurée_ou_aucune_source")
        return None
    evidence = "\n\n".join(
        f"SOURCE S{index}: {material.source.name} — {material.title}\n"
        f"DATE: {material.source.published_at.date().isoformat() if material.source.published_at else 'non indiquée'}\n"
        f"URL: {material.source.url}\nORIGINE: {material.origin}\nTEXTE: {material.text}"
        for index, material in enumerate(materials[:10], 1)
    )
    lower, upper = target
    revision_text = ""
    if revision:
        draft, critique = revision
        revision_text = ("\n\nRÉÉCRITURE FINALE : réécris réellement l'article, supprime les "
                         "répétitions, améliore les transitions, intègre les nouvelles sources et "
                         "préserve uniquement les faits correctement sourcés. Les source_ids du draft "
                         "réfèrent à draft.sources ; dans ta nouvelle réponse, source_ids doivent "
                         "référer aux identifiants SOURCE S1, S2 du dossier ci-dessus.\n"
                         "DRAFT: " + draft.model_dump_json() + "\nCRITIQUE: " + critique.model_dump_json())
    category_guidance = ""
    if category == "Mythologies & Religions":
        category_guidance = ("Approche descriptive, historique, comparative si pertinent et non "
                             "confessionnelle. Distingue explicitement texte, tradition, "
                             "interprétation historique et analyse moderne. Respecte les traditions. ")
    elif category == "Histoire":
        category_guidance = ("Dossier historique intemporel : reconstitue chronologie, acteurs, "
                             "causes et conséquences uniquement si les sources les documentent. ")
    raw = _chat([
        {"role": "system", "content": LONG_SYSTEM},
        {"role": "user", "content": (
            f"Rubrique : {category}. Format : {tier}. Vise {lower} à {upper} mots "
            "seulement si les matériaux le permettent. Ne répète pas la même idée "
            "pour atteindre ce nombre. " + category_guidance + "\n\n" + evidence + revision_text
        )},
    ])
    _mark(received=True)
    if raw == "INSUFFISANT":
        _mark(validation="REJET", reason="llm_insufficient")
        logger.warning("%s : l'IA signale des sources insuffisantes", category)
        return None
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
        parts = data["paragraphs"]
        if not isinstance(parts, list) or not parts:
            raise ValueError("paragraphs_empty_or_invalid")
        normalized_parts = []
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("paragraph_invalid")
            part = dict(part)
            if not isinstance(part.get("text"), str) or not part["text"].strip():
                _mark(validation="REJET", reason="empty_section")
                return None
            ids = part.get("source_ids")
            if not isinstance(ids, list):
                _mark(validation="REJET", reason="source_ids_missing")
                return None
            parsed_ids = []
            for value in ids:
                match = re.fullmatch(r"(?:\[)?S?(\d+)(?:\])?", str(value), re.IGNORECASE)
                if not match or not 1 <= int(match.group(1)) <= min(len(materials), 10):
                    _mark(validation="REJET", reason="invalid_source_reference")
                    reference = str(value) if re.fullmatch(r"\[?S?\d+\]?", str(value), re.I) else "non_numérique"
                    logger.warning("[Validation] phase=%s reference=%s available=S1..S%d",
                                   _trace.get().phase if _trace.get() else "draft", reference,
                                   min(len(materials), 10))
                    return None
                parsed_ids.append(int(match.group(1)))
            part["source_ids"] = parsed_ids
            if not part.get("dimensions") and "kind" in part:
                part["dimensions"] = [part["kind"]]
            normalized_parts.append(part)
        paragraphs = [ArticleParagraph.model_validate(part) for part in normalized_parts]
    except (ValueError, KeyError, TypeError, IndexError) as error:
        if str(error) in {"paragraphs_empty_or_invalid", "paragraph_invalid"}:
            reason = "empty_section" if str(error) == "paragraphs_empty_or_invalid" else "malformed_response"
            _mark(validation="REJET", reason=reason)
            logger.warning("%s : réponse IA rejetée, %s", category, str(error))
            return None
        _mark(validation="REJET", reason="malformed_response")
        if hasattr(error, "errors"):
            for issue in error.errors():
                logger.warning("[Validation] phase=%s field=%s type=%s",
                               _trace.get().phase if _trace.get() else "draft",
                               ".".join(str(x) for x in issue.get("loc", ())), issue.get("type", "invalid"))
        raise
    _mark(words=sum(len(part.text.split()) for part in paragraphs))
    headings = [part.heading.casefold() for part in paragraphs if part.heading]
    if len(headings) != len(set(headings)):
        _mark(validation="REJET", reason="duplicated_heading")
        logger.warning("[Validation] phase=%s heading=%s",
                       _trace.get().phase if _trace.get() else "draft",
                       next(heading for heading in headings if headings.count(heading) > 1)[:100])
        return None
    normalized = [" ".join(part.text.casefold().split()) for part in paragraphs]
    if (revision or headings) and len(normalized) != len(set(normalized)):
        _mark(validation="REJET", reason="duplicate_paragraphs")
        return None
    required = {"facts", "context", "mechanisms", "analysis", "consequences", "limits"}
    covered = {dimension for part in paragraphs for dimension in part.dimensions}
    if not required.issubset(covered):
        missing = ",".join(sorted(required - covered))
        _mark(validation="REJET", reason="missing_editorial_dimension")
        logger.warning("[Validation] phase=%s missing=%s",
                       _trace.get().phase if _trace.get() else "draft", missing)
        logger.warning("%s : réponse IA rejetée, dimensions manquantes : %s", category, missing)
        return None
    for part in paragraphs:
        if not part.source_ids or any(i < 1 or i > len(materials) for i in part.source_ids):
            _mark(validation="REJET", reason="invalid_source_ids")
            logger.warning("%s : réponse IA rejetée, référence de source absente ou invalide", category)
            return None
    if v2 and (not 2 <= len(headings) <= 4 or not paragraphs[0].heading):
        if len(paragraphs) < 3:
            _mark(validation="REJET", reason="invalid_section_count")
            logger.warning("[Validation] phase=%s headings=%d paragraphs=%d",
                           _trace.get().phase if _trace.get() else "draft", len(headings), len(paragraphs))
            return None
        starts = {0: "Contexte et faits", len(paragraphs) // 3: "Mécanismes et enjeux",
                  2 * len(paragraphs) // 3: "Conséquences et limites"}
        paragraphs = [part.model_copy(update={"heading":
                      (part.heading or starts[index]) if index in starts else ""})
                      for index, part in enumerate(paragraphs)]
        logger.info("[Validation] phase=%s headings_normalized=%d->3 paragraphs=%d",
                    _trace.get().phase if _trace.get() else "draft", len(headings), len(paragraphs))
    elif revision and not 2 <= len(headings) <= 4:
        _mark(validation="REJET", reason="invalid_section_count")
        return None
    used = sorted({index for paragraph in paragraphs for index in paragraph.source_ids})
    remap = {old: new for new, old in enumerate(used, 1)}
    paragraphs = [paragraph.model_copy(update={"source_ids": [remap[index] for index in paragraph.source_ids]})
                  for paragraph in paragraphs]
    cited_materials = [materials[index - 1] for index in used]
    try:
        article = FeatureArticle(
            category=category, title=data["title"], tier=tier, paragraphs=paragraphs,
            sources=[material.source for material in cited_materials],
            attribution=" ".join(dict.fromkeys(material.license_note for material in cited_materials
                                                if material.license_note)),
        )
    except (ValueError, KeyError, TypeError, IndexError):
        _mark(validation="REJET", reason="invalid_article_structure")
        raise
    _mark(words=article.word_count())
    if article.word_count() > upper:
        _mark(validation="REJET", reason="above_word_limit")
        logger.warning("%s : réponse IA rejetée, %d mots dépassent le maximum %d",
                       category, article.word_count(), upper)
        return None
    if article.word_count() < max(180, lower // 2):
        _mark(validation="REJET", reason="below_safety_minimum")
        logger.warning("%s : réponse IA rejetée, %d mots sous le minimum de sécurité",
                       category, article.word_count())
        return None
    if article.word_count() < lower:
        article = article.model_copy(update={"shortfall": True,
                                             "shortfall_reasons": ["article_short"]})
    _mark(validation="OK", reason="article_short" if article.word_count() < lower else "")
    return article


class EditorialCritique(BaseModel):
    repetition: float = Field(ge=0, le=1)
    continuity: float = Field(ge=0, le=1)
    clarity: float = Field(ge=0, le=1)
    depth: float = Field(ge=0, le=1)
    source_diversity: float = Field(ge=0, le=1)
    unsupported_claims: list[str]
    repeated_points: list[str]
    missing_context: list[str]
    missing_questions: list[str]
    weak_passages: list[str]
    additional_research_needed: bool
    suggested_search_queries: list[str]


def _critique_response_format() -> dict | None:
    """Schéma strict uniquement pour l'API OpenAI qui le prend en charge."""
    endpoint = urlsplit(os.environ.get("SIGNAL_MATIN_LLM_URL", "")).hostname
    model = os.environ.get("SIGNAL_MATIN_LLM_MODEL", "").strip()
    if endpoint != "api.openai.com" or not model.startswith("gpt-4.1-mini"):
        return None
    scores = ("repetition", "continuity", "clarity", "depth", "source_diversity")
    lists = ("unsupported_claims", "repeated_points", "missing_context",
             "missing_questions", "weak_passages", "suggested_search_queries")
    properties = {key: {"type": "number"} for key in scores}
    properties.update({key: {"type": "array", "items": {"type": "string"}} for key in lists})
    properties["additional_research_needed"] = {"type": "boolean"}
    return {"type": "json_schema", "json_schema": {
        "name": "signal_matin_editorial_critique", "strict": True,
        "schema": {"type": "object", "properties": properties,
                   "required": list(properties), "additionalProperties": False}}}


def critique_feature(article: FeatureArticle, materials: list[Material]) -> EditorialCritique:
    """Évalue le draft sans réécrire ; une sortie invalide bloque la publication."""
    evidence = "\n".join(f"[S{i}] {m.source.name} | {m.source.url} | {m.text[:12000]}"
                         for i, m in enumerate(materials[:10], 1))
    raw = _chat([
        {"role": "system", "content": (
            "Tu es critique éditorial. N'écris aucun article. Retourne uniquement un objet JSON. "
            "Évalue repetition (1 = beaucoup), continuity, clarity, depth, source_diversity "
            "(1 = excellent). Repère affirmations non étayées, répétitions, transitions faibles, "
            "superficialité, contradictions absentes et conclusion qui répète l'introduction. "
            "Les source_ids de l'article indexent article.sources ; les [n] du dossier indexent "
            "le dossier complet. Compare les URL pour relier les deux. "
            "Suggère des requêtes précises, non redondantes, seulement pour les lacunes documentaires.")},
        {"role": "user", "content": "ARTICLE: " + article.model_dump_json() + "\nSOURCES:\n" + evidence +
         "\nChamps JSON obligatoires : repetition, continuity, clarity, depth, source_diversity, "
         "unsupported_claims, repeated_points, missing_context, missing_questions, weak_passages, "
         "additional_research_needed, suggested_search_queries. Types attendus : "
         "repetition/continuity/clarity/depth/source_diversity = nombres entre 0 et 1 ; "
         "unsupported_claims/repeated_points/missing_context/missing_questions/weak_passages/"
         "suggested_search_queries = listes de chaînes, même vides ; "
         "additional_research_needed = booléen. Aucun champ omis."},
    ], response_format=_critique_response_format())
    try:
        return EditorialCritique.model_validate_json(
            raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except ValidationError as error:
        for issue in error.errors():
            logger.warning("[Validation] phase=critique reason=invalid_critique field=%s type=%s",
                           ".".join(str(x) for x in issue.get("loc", ())), issue.get("type", "invalid"))
        raise
