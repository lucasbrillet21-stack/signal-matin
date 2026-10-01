"""Exercise today's public-source pipeline with a simulated writer, without PDF or email."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from signal_matin.config import load_config  # noqa: E402
from signal_matin.editorial import categories_for_date, local_date  # noqa: E402
from signal_matin.pipeline import build_live  # noqa: E402
from signal_matin.renderer import render_html  # noqa: E402


def simulated_chat(messages: list[dict[str, str]]) -> str:
    """Return structural test text only; it is never saved or published."""
    prompt = messages[-1]["content"]
    match = re.search(r"Vise (\d+) à (\d+) mots", prompt)
    if not match or "SOURCE 1:" not in prompt or "TEXTE:" not in prompt:
        raise AssertionError("The writer did not receive documented source material")
    lower, upper = map(int, match.groups())
    count = 8 if lower >= 700 else 6
    target = (lower + upper) // 2
    sentence = ("Simulation locale de validation du parcours documentaire. "
                "Ce texte ne constitue pas un article et ne doit pas être publié.")
    kinds = ["facts", "context", "mechanisms", "analysis", "consequences", "limits"]
    paragraphs = []
    for index in range(count):
        words = (sentence.split() * 20)[:target // count]
        paragraphs.append({"kind": kinds[min(index, 5)], "text": " ".join(words),
                           "source_ids": [1]})
    return json.dumps({"title": "Simulation locale non publiable", "paragraphs": paragraphs})


def main() -> None:
    config = load_config("config.personal.example.yaml")
    now = dt.datetime.now(dt.timezone.utc)
    date = local_date(config, now)
    selected = categories_for_date(config, date)
    with patch("signal_matin.editorial.llm_configured", return_value=True), \
         patch("signal_matin.synthesis.llm_configured", return_value=True), \
         patch("signal_matin.synthesis._chat", side_effect=simulated_chat):
        edition = build_live(config, now=now)
    html = render_html(edition)
    if "Simulation locale non publiable" not in html and edition.personal_features:
        raise AssertionError("The simulated article is missing from the in-memory rendering")
    print(f"Date Sydney : {date}; rotation : {', '.join(selected)}")
    print(f"Entrées RSS retenues : {len(edition.personal_articles)}")
    for feature in edition.personal_features:
        print(f"{feature.category} : {feature.word_count()} mots, "
              f"{len(feature.sources)} source(s) citée(s), format {feature.tier}")
        for source in feature.sources:
            print(f"  {source.name} — {source.title} — {source.url}")
    print(f"Pensée du jour : {'oui' if edition.thought else 'non'}")
    if not edition.personal_features:
        raise SystemExit("Dry-run échoué : aucun dossier n'a franchi la validation")


if __name__ == "__main__":
    main()
