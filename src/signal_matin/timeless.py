"""Sujets intemporels choisis avant la collecte documentaire."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import yaml

from .config import ROOT, setting
from .models import NewsItem, SourceRef

TIMELESS = {"Histoire", "Mythologies & Religions", "Philosophie"}


def topics_for_date(config: dict, category: str, date: dt.date) -> list[NewsItem]:
    topics = setting(config, f"editorial.topic_bank.{category}", []) or []
    if not topics:
        bank_file = ROOT / str(setting(config, "editorial.topic_bank_file", "topics.v2.yaml"))
        if bank_file.is_file():
            topics = (yaml.safe_load(bank_file.read_text(encoding="utf-8")) or {}).get(category, [])
    if not topics:
        return []
    path = Path(os.environ.get("SIGNAL_MATIN_TOPIC_HISTORY",
                               str(ROOT / "output" / "state" / "timeless-topics.json")))
    try:
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as error:
        raise ValueError("historique des sujets intemporels illisible") from error
    recent = {row["title"] for row in history.get(category, [])
              if (date - dt.date.fromisoformat(row["date"])).days in range(0, 91)}
    start = date.toordinal() % len(topics)
    ordered = [topics[(start + offset) % len(topics)] for offset in range(len(topics))]
    return [NewsItem(title=str(topic["title"]), category=category,
                     summary=f"Sujet documentaire intemporel à vérifier : {topic['title']}. "
                             f"Angle : {topic['scope']}. Ce descriptif est une intention de recherche, "
                             "pas une source factuelle.",
                     source=SourceRef(name="Banque de sujets Signal Matin", title=str(topic["title"])))
            for topic in ordered if topic["title"] not in recent]


def topic_for_date(config: dict, category: str, date: dt.date) -> NewsItem | None:
    return next(iter(topics_for_date(config, category, date)), None)


def remember_published(category: str, title: str, date: dt.date) -> None:
    path = Path(os.environ.get("SIGNAL_MATIN_TOPIC_HISTORY",
                               str(ROOT / "output" / "state" / "timeless-topics.json")))
    try:
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as error:
        raise ValueError("historique des sujets intemporels illisible") from error
    history.setdefault(category, []).append({"date": date.isoformat(), "title": title})
    history[category] = history[category][-200:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
