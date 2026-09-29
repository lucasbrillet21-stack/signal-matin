"""Courtes citations contrôlées dans une édition ancienne du domaine public."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .models import ThoughtOfDay


def thought_for_date(date: dt.date) -> ThoughtOfDay | None:
    entries = json.loads(Path(__file__).with_name("quotes.json").read_text(encoding="utf-8"))
    verified = [entry for entry in entries if entry.get("verified") is True]
    if not verified:
        return None
    # La rotation évite toute répétition dans les jours consécutifs.
    entry = verified[date.toordinal() % len(verified)]
    values = {key: value for key, value in entry.items()
              if key not in {"verified", "expansion"}}
    values["explanation"] = f"{entry['explanation']} {entry.get('expansion', '')}".strip()
    return ThoughtOfDay.model_validate(values)
