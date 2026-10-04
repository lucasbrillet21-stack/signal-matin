"""Rédaction d'articles longs depuis des matériaux sourcés via API chat compatible."""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from pydantic import BaseModel, Field

from .api_cost import record_llm_usage
from .models import ArticleParagraph, FeatureArticle
from .source_material import Material

logger = logging.getLogger(__name__)


@dataclass
class ComposeTrace:
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


def llm_configured() -> bool:
    return all(os.environ.get(key, "").strip() for key in (
        "SIGNAL_MATIN_LLM_URL", "SIGNAL_MATIN_LLM_API_KEY", "SIGNAL_MATIN_LLM_MODEL"
    ))


def _chat(messages: list[dict[str, str]]) -> str:
    endpoint = os.environ["SIGNAL_MATIN_LLM_URL"].strip()
    if not endpoint.startswith("https://"):
        raise ValueError("SIGNAL_MATIN_LLM_URL doit utiliser HTTPS")
    payload = json.dumps({
        "model": os.environ["SIGNAL_MATIN_LLM_MODEL"].strip(),
        "temperature": 0,
        "messages": messages,
    }, ensure_ascii=False).encode("utf-8")
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
Organise le texte en environ trois grandes parties cohérentes, chacune avec un titre
unique et plusieurs paragraphes fluides si nécessaire. Couvre contexte, faits,
mécanismes, analyse, conséquences et limites sans répéter labels ni informations.
Ne reviens pas artificiellement aux faits déjà exposés.
Réponds uniquement en JSON : {"title":"...","paragraphs":[{"kind":"facts|context|mechanisms|analysis|consequences|limits","heading":"titre de partie ou vide pour la suite","text":"...","source_ids":[1]}]}.
Écris des paragraphes de 50 à 140 mots ; conserve des transitions naturelles.
La conclusion doit exposer au moins une limite ou incertitude documentée.
"""


def compose_feature(
    category: str, tier: str, materials: list[Material], target: tuple[int, int],
    revision: tuple[FeatureArticle, "EditorialCritique"] | None = None,
) -> FeatureArticle | None:
    """Rédige depuis des matériaux réels ; toute sortie est validée avant publication."""
    if not llm_configured() or not materials:
        _mark(validation="REJET", reason="api_non_configurée_ou_aucune_source")
        return None
    evidence = "\n\n".join(
        f"SOURCE {index}: {material.source.name} — {material.title}\n"
        f"DATE: {material.source.published_at.date().isoformat() if material.source.published_at else 'non indiquée'}\n"
        f"URL: {material.source.url}\nORIGINE: {material.origin}\nTEXTE: {material.text}"
        for index, material in enumerate(materials[:5], 1)
    )
    lower, upper = target
    revision_text = ""
    if revision:
        draft, critique = revision
        revision_text = ("\n\nRÉÉCRITURE FINALE : réécris réellement l'article, supprime les "
                         "répétitions, améliore les transitions, intègre les nouvelles sources et "
                         "préserve uniquement les faits correctement sourcés. Les source_ids du draft "
                         "réfèrent à draft.sources ; dans ta nouvelle réponse, source_ids doivent "
                         "référer aux SOURCE numérotées du dossier ci-dessus.\n"
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
        paragraphs = [ArticleParagraph.model_validate(part) for part in data["paragraphs"]]
    except (ValueError, KeyError, TypeError, IndexError):
        _mark(validation="REJET", reason="invalid_json_or_structure")
        raise
    _mark(words=sum(len(part.text.split()) for part in paragraphs))
    headings = [part.heading.casefold() for part in paragraphs if part.heading]
    if len(headings) != len(set(headings)):
        _mark(validation="REJET", reason="duplicate_headings")
        return None
    if revision and not 2 <= len(headings) <= 4:
        _mark(validation="REJET", reason="invalid_section_count")
        return None
    normalized = [" ".join(part.text.casefold().split()) for part in paragraphs]
    if (revision or headings) and len(normalized) != len(set(normalized)):
        _mark(validation="REJET", reason="duplicate_paragraphs")
        return None
    required = {"facts", "context", "mechanisms", "analysis", "consequences", "limits"}
    if not required.issubset({part.kind for part in paragraphs}):
        _mark(validation="REJET", reason="missing_sections")
        logger.warning("%s : réponse IA rejetée, sections obligatoires manquantes", category)
        return None
    for part in paragraphs:
        if not part.source_ids or any(i < 1 or i > len(materials) for i in part.source_ids):
            _mark(validation="REJET", reason="invalid_source_ids")
            logger.warning("%s : réponse IA rejetée, référence de source absente ou invalide", category)
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


def critique_feature(article: FeatureArticle, materials: list[Material]) -> EditorialCritique:
    """Évalue le draft sans réécrire ; une sortie invalide bloque la publication."""
    evidence = "\n".join(f"[{i}] {m.source.name} | {m.source.url} | {m.text[:12000]}"
                         for i, m in enumerate(materials[:5], 1))
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
         "additional_research_needed, suggested_search_queries."},
    ])
    return EditorialCritique.model_validate_json(
        raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip())
