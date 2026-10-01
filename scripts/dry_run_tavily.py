"""Dry-run hors réseau : RSS 300 caractères -> Tavily et GPT simulés."""
from __future__ import annotations

import datetime as dt
import json
import os
from unittest.mock import patch

from signal_matin.config import load_config
from signal_matin.connectors.tavily import SearchHit
from signal_matin.editorial import write_features
from signal_matin.models import NewsItem, SourceRef
from signal_matin.source_material import Material
from signal_matin.synthesis import compose_feature


def main() -> None:
    date = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
    category = "Ingénierie"
    title = "Exercice fictif : Aster teste Delta III après un incident simulé"
    summary = ("Aster indique, dans cet exercice fictif, que le module Delta III doit "
               "subir de nouveaux essais après un incident simulé. La date, les "
               "conséquences et les limites restent à vérifier. ")[:300].ljust(300, " ")
    rss = NewsItem(title=title, category=category, summary=summary,
                   source=SourceRef(name="Aster fictif", title=title,
                                    url="https://aster.example/exercice", published_at=date))
    hits = [
        SearchHit("Aster teste Delta III après incident simulé : protocole", "https://primary.example/protocole",
                  "Delta III incident simulé. " + "Le protocole fictif définit des étapes techniques distinctes. " * 35,
                  "primary.example", date, 0.95),
        SearchHit("Delta III : Aster détaille l'incident simulé et les limites", "https://review.example/limites",
                  "Aster Delta III incident simulé. " + "Une analyse fictive précise les incertitudes restantes. " * 35,
                  "review.example", date, 0.88),
    ]
    captured: list[Material] = []

    def material(item: NewsItem) -> Material:
        return Material(item.source, item.title, item.expanded_summary or item.summary,
                        origin="rss" if str(item.source.url) == str(rss.source.url) else "tavily")

    def writer(category, tier, materials, target):
        captured.extend(materials)
        return compose_feature(category, tier, materials, target)

    def chat(_messages):
        kinds = ("facts", "context", "mechanisms", "analysis", "consequences", "limits")
        words = ("Dans cet exercice technique fictif, les sources simulées décrivent "
                 "le protocole, ses conséquences et les limites de la documentation disponible. ").split()
        paragraphs = [{"kind": kind, "text": " ".join((words * 12)[:120]),
                       "source_ids": [(index % 3) + 1]} for index, kind in enumerate(kinds)]
        return json.dumps({"title": title, "paragraphs": paragraphs}, ensure_ascii=False)

    env = {"TAVILY_API_KEY": "SIMULATED-ONLY", "SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
           "SIGNAL_MATIN_LLM_MODEL": "SIMULATED-ONLY", "SIGNAL_MATIN_LLM_API_KEY": "SIMULATED-ONLY"}
    with patch.dict(os.environ, env), \
         patch("signal_matin.editorial.article_material", side_effect=material), \
         patch("signal_matin.editorial.tavily_search", return_value=hits) as search, \
         patch("signal_matin.editorial.compose_feature", side_effect=writer), \
         patch("signal_matin.synthesis._chat", side_effect=chat) as gpt:
        articles = write_features(load_config("config.personal.example.yaml"), date.date(),
                                  [category], [rss])
    if len(articles) != 1 or sum(len(source.text) for source in captured) < 3000:
        raise SystemExit("Dry-run échoué : dossier documentaire insuffisant")
    print("SIMULATION TECHNIQUE — aucun appel réseau, Tavily, OpenAI ou email réel")
    print(f"RSS initial : {len(summary)} caractères / 1 source")
    print(f"Dossier final : {sum(len(source.text) for source in captured)} caractères / {len(captured)} sources")
    print(f"Recherches Tavily simulées : {search.call_count} ; appels GPT simulés : {gpt.call_count}")
    print(f"Article validé : {articles[0].word_count()} mots ; sources citées : {len(articles[0].sources)}")


if __name__ == "__main__":
    main()
