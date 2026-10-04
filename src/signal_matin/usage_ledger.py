"""Compteur local des crédits Tavily connus, par mois Sydney."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ROOT


class MonthlyCreditLedger:
    def __init__(self, month: str, limit: int, path: Path | None = None):
        self.month = month
        self.limit = max(0, limit)
        self.path = path or Path(os.environ.get("SIGNAL_MATIN_TAVILY_LEDGER",
                                             str(ROOT / "output" / "state" / "tavily-credits.json")))

    def used(self) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("month") == self.month:
                return max(0, int(data.get("credits", 0)))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise ValueError("compteur Tavily local illisible") from error
        return 0

    def reserve(self, credits: int = 1) -> bool:
        """Réserve prudemment avant la requête ; une réponse absente ne libère rien."""
        used = self.used()
        if used + credits > self.limit:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"month": self.month, "credits": used + credits}),
                             encoding="utf-8")
        temporary.replace(self.path)
        return True

    def add_reported_extra(self, credits: int) -> None:
        """Réconcilie une réponse qui annonce plus que le crédit réservé."""
        if credits <= 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"month": self.month, "credits": self.used() + credits}),
                             encoding="utf-8")
        temporary.replace(self.path)
