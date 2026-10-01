"""Diagnostic ponctuel : un sujet RSS réel, une seule recherche Tavily, aucune rédaction."""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from signal_matin.config import load_config, setting
from signal_matin.connectors.rss import collect_rss
from signal_matin.connectors.tavily import TavilyError, search as tavily_search
from signal_matin.editorial import (
    EVERGREEN_CATEGORIES, _discovery_ready, _document_chars, _independent_sources,
    _rich_enough, _search_query, filter_tavily_hits,
)
from signal_matin.models import NewsItem
from signal_matin.source_material import article_material


class DiagnosticError(Exception):
    """Le diagnostic ne peut pas démarrer faute de sujet RSS exploitable."""


def select_subject(config: dict, now: dt.datetime) -> NewsItem:
    """S'arrête au premier sujet daté et identifiable d'un flux configuré."""
    feeds = setting(config, "news.feeds", []) or []
    max_age = max(48, int(setting(config, "news.max_age_hours", 48) or 48))
    for feed in feeds:
        if not isinstance(feed, dict):
            continue
        items, _ = collect_rss([feed], now, limit=8, max_age_hours=max_age,
                               status_name="Diagnostic RSS", require_date=True)
        for item in items:
            if _discovery_ready(item, config):
                return item
    raise DiagnosticError("aucun sujet RSS récent avec au moins 120 caractères exploitables")


def _safe_line(value: str) -> str:
    return " ".join(value.split())[:240]


def _safe_url(url: str) -> str:
    """Conserve l'URL en mémoire ; masque seulement d'éventuels jetons dans le log."""
    parts = urlsplit(url)
    query = urlencode([(name, value) for name, value in parse_qsl(parts.query)
                       if not any(word in name.casefold() for word in
                                  ("token", "secret", "password", "api_key", "auth"))])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def run_diagnostic(config: dict, now: dt.datetime) -> None:
    if not os.environ.get("TAVILY_API_KEY", "").strip():
        raise TavilyError("clé TAVILY_API_KEY absente")
    subject = select_subject(config, now)
    initial = article_material(subject)
    materials = [initial]
    query = _search_query(subject)
    print("[Tavily diagnostic]", flush=True)
    print(f"Sujet : {_safe_line(subject.title)}", flush=True)
    print(f"Rubrique : {_safe_line(subject.category)}", flush=True)
    print(f"Requête : {_safe_line(query)}", flush=True)
    print(f"Matière avant Tavily : {_document_chars(materials)} caractères", flush=True)
    print("Recherche Tavily : 1/1", flush=True)
    hits = tavily_search(query, max_results=8,
                         recent=subject.category not in EVERGREEN_CATEGORIES)
    print(f"Résultats reçus : {len(hits)}", flush=True)
    filter_tavily_hits(subject, materials, hits)
    retained = materials[1:]
    print(f"Résultats retenus : {len(retained)}", flush=True)
    for material in retained:
        url = str(material.source.url or "")
        print(f"- Titre : {_safe_line(material.title)}", flush=True)
        print(f"  Domaine : {urlsplit(url).hostname or 'inconnu'}", flush=True)
        print(f"  URL : {_safe_url(url)}", flush=True)
        print(f"  Contenu documentaire : {len(material.text.strip())} caractères", flush=True)
    chars = _document_chars(materials)
    enough = chars >= 900
    rich = _rich_enough(materials, "dossier", config)
    print(f"Matière après Tavily : {chars} caractères", flush=True)
    print(f"Nombre de domaines indépendants : {_independent_sources(materials)}", flush=True)
    print(f"Seuil documentaire >= 900 : {'OUI' if enough else 'NON'}", flush=True)
    print(f"Objectif de richesse atteint : {'OUI' if rich else 'NON'}", flush=True)
    print(f"Dossier suffisamment documenté : {'OUI' if enough else 'NON'}", flush=True)


def main() -> int:
    logging.getLogger("signal_matin").setLevel(logging.CRITICAL)
    try:
        run_diagnostic(load_config("config.personal.example.yaml"),
                       dt.datetime.now(dt.timezone.utc))
    except DiagnosticError as error:
        print(f"[Tavily diagnostic]\nNon exécuté : {error}\nRecherche Tavily : 0/1")
        return 0
    except TavilyError as error:
        print(f"Diagnostic impossible : {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Erreur technique du diagnostic : {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
