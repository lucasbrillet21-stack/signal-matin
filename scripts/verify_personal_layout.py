"""Produit une maquette PDF explicitement fictive pour contrôler la pagination."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_personal import fixture_feature  # noqa: E402
from signal_matin.models import EditionMeta, MorningEdition  # noqa: E402
from signal_matin.pdf import generer_pdf  # noqa: E402
from signal_matin.thought import thought_for_date  # noqa: E402


def main() -> None:
    now = dt.datetime.now(ZoneInfo("Australia/Sydney"))
    categories = ["International", "Géopolitique", "Économie"]
    edition = MorningEdition(
        generated_at=now, demo=True, personal_journal=True,
        edition=EditionMeta(
            date=now.date(), number=1, title="Signal Matin",
            subtitle="MAQUETTE TECHNIQUE — TEXTE FICTIF",
            motto="Validation de la mise en page ; aucune actualité réelle.",
        ),
        thought=thought_for_date(now.date()), expected_categories=categories,
        personal_features=[
            fixture_feature(categories[0], "dossier", 800),
            fixture_feature(categories[1], "article", 500),
            fixture_feature(categories[2], "lecture", 360),
        ],
    )
    path = ROOT / "output" / "pdf" / "verification-maquette-personnelle.pdf"
    generer_pdf(edition, path,
                html_path=ROOT / "output" / "preview" / "verification-maquette-personnelle.html")
    print(f"Maquette fictive générée : {path}")


if __name__ == "__main__":
    main()
