"""Rédaction d'articles longs depuis des matériaux sourcés via API chat compatible."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from .models import ArticleParagraph, FeatureArticle
from .source_material import Material

logger = logging.getLogger(__name__)


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
        return json.load(response)["choices"][0]["message"]["content"].strip()


LONG_SYSTEM = """Tu es rédacteur d'un journal personnel en français. Écris un article de fond original.
Utilise uniquement les matériaux numérotés fournis. N'invente jamais de fait, date,
chiffre, citation, relation causale ou source. Ne reprends jamais plusieurs phrases
consécutives d'une source. N'accède à aucune connaissance non présente dans les
matériaux pour combler un manque. Si la matière est insuffisante, écris plus court
ou réponds exactement INSUFFISANT. Distingue les faits rapportés, le contexte,
les mécanismes, les conséquences, l'analyse et les limites. Sur les sujets
politiques, reste neutre et présente les perspectives présentes dans les sources
sans attribuer d'intentions. Chaque paragraphe doit citer au moins un numéro de
source existant. Ne produis aucune citation directe non fournie.
Réponds uniquement en JSON : {"title":"...","paragraphs":[{"kind":"facts|context|mechanisms|analysis|consequences|limits","text":"...","source_ids":[1]}]}.
Écris des paragraphes de 50 à 140 mots ; conserve des transitions naturelles.
La conclusion doit exposer au moins une limite ou incertitude documentée.
"""


def compose_feature(
    category: str, tier: str, materials: list[Material], target: tuple[int, int],
) -> FeatureArticle | None:
    """Rédige depuis des matériaux réels ; toute sortie est validée avant publication."""
    if not llm_configured() or not materials:
        return None
    evidence = "\n\n".join(
        f"SOURCE {index}: {material.source.name} — {material.title}\n"
        f"DATE: {material.source.published_at.date().isoformat() if material.source.published_at else 'non indiquée'}\n"
        f"URL: {material.source.url}\nTEXTE: {material.text}"
        for index, material in enumerate(materials[:5], 1)
    )
    lower, upper = target
    raw = _chat([
        {"role": "system", "content": LONG_SYSTEM},
        {"role": "user", "content": (
            f"Rubrique : {category}. Format : {tier}. Vise {lower} à {upper} mots "
            "seulement si les matériaux le permettent. Ne répète pas la même idée "
            "pour atteindre ce nombre.\n\n" + evidence
        )},
    ])
    if raw == "INSUFFISANT":
        logger.warning("%s : l'IA signale des sources insuffisantes", category)
        return None
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    paragraphs = [ArticleParagraph.model_validate(part) for part in data["paragraphs"]]
    required = {"facts", "context", "mechanisms", "analysis", "consequences", "limits"}
    if not required.issubset({part.kind for part in paragraphs}):
        logger.warning("%s : réponse IA rejetée, sections obligatoires manquantes", category)
        return None
    for part in paragraphs:
        if not part.source_ids or any(i < 1 or i > len(materials) for i in part.source_ids):
            logger.warning("%s : réponse IA rejetée, référence de source absente ou invalide", category)
            return None
    used = sorted({index for paragraph in paragraphs for index in paragraph.source_ids})
    remap = {old: new for new, old in enumerate(used, 1)}
    paragraphs = [paragraph.model_copy(update={"source_ids": [remap[index] for index in paragraph.source_ids]})
                  for paragraph in paragraphs]
    cited_materials = [materials[index - 1] for index in used]
    article = FeatureArticle(
        category=category, title=data["title"], tier=tier, paragraphs=paragraphs,
        sources=[material.source for material in cited_materials],
        attribution=" ".join(dict.fromkeys(material.license_note for material in cited_materials
                                            if material.license_note)),
    )
    if article.word_count() > upper:
        logger.warning("%s : réponse IA rejetée, %d mots dépassent le maximum %d",
                       category, article.word_count(), upper)
        return None
    if article.word_count() < max(180, lower // 2):
        logger.warning("%s : réponse IA rejetée, %d mots sous le minimum de sécurité",
                       category, article.word_count())
        return None
    if article.word_count() < lower:
        article = article.model_copy(update={"shortfall": True})
    return article
