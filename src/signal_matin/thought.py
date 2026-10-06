"""Courtes citations vérifiées et rotation de la pensée du jour."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .config import ROOT
from .models import ThoughtOfDay


def quote_history_path() -> Path:
    return Path(os.environ.get("SIGNAL_MATIN_QUOTE_HISTORY",
                               str(ROOT / "output" / "state" / "quote-history.json")))


def _history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("format invalide")
        return rows
    except (OSError, ValueError) as error:
        raise ValueError("historique des pensées illisible") from error


def thought_for_date(date: dt.date, *, history_path: Path | None = None) -> ThoughtOfDay | None:
    entries = json.loads(Path(__file__).with_name("quotes.json").read_text(encoding="utf-8"))
    verified = [entry for entry in entries if entry.get("verified") is True]
    if not verified:
        return None
    by_author: dict[str, list[dict]] = {}
    for entry in verified:
        by_author.setdefault(entry["author"], []).append(entry)
    ordered = []
    previous_author = None
    while any(by_author.values()):
        author = min((name for name, quotes in by_author.items() if quotes),
                     key=lambda name: (name == previous_author, -len(by_author[name]), name))
        ordered.append(by_author[author].pop(0))
        previous_author = author
    history = [row for row in (_history(history_path) if history_path is not None else [])
               if row.get("date", "") < date.isoformat()]
    used = {row.get("text") for row in history}
    last_author = max(history, key=lambda row: row["date"]).get("author") if history else None
    start = date.toordinal() % len(ordered)
    candidates = ordered[start:] + ordered[:start]
    unseen = [item for item in candidates if item["text"] not in used]
    if not history:
        entry = candidates[0]
    elif unseen:
        remaining = {author: sum(item["author"] == author for item in unseen)
                     for author in {item["author"] for item in unseen}}
        entry = min(unseen, key=lambda item: (item["author"] == last_author,
                                              -remaining[item["author"]], candidates.index(item)))
    else:
        # Après épuisement, reprendre la plus ancienne en alternant les auteurs.
        last_used = {row.get("text"): row.get("date", "") for row in history}
        entry = min(candidates, key=lambda item: (item["author"] == last_author,
                                                  last_used.get(item["text"], "")))
    values = {key: value for key, value in entry.items()
              if key not in {"verified", "expansion"}}
    values["explanation"] = f"{entry['explanation']} {entry.get('expansion', '')}".strip()
    return ThoughtOfDay.model_validate(values)


def remember_thought(date: dt.date, thought: ThoughtOfDay, *, history_path: Path | None = None) -> None:
    path = history_path or quote_history_path()
    rows = [row for row in _history(path) if row.get("date") != date.isoformat()]
    rows.append({"date": date.isoformat(), "text": thought.text, "author": thought.author})
    rows = sorted(rows, key=lambda row: row["date"])[-200:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
