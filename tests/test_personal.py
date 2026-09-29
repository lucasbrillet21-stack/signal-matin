"""Rotation, rédaction et pagination sans réseau ni courrier sortant."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

from scripts.should_run_sydney import should_run
from signal_matin.config import load_config
from signal_matin.connectors.rss import collect_rss
from signal_matin.editorial import categories_for_date, local_date
from signal_matin.models import ArticleParagraph, FeatureArticle, SourceRef
from signal_matin.pdf import generer_pdf, inspecter_html
from signal_matin.pipeline import build_live
from signal_matin.renderer import render_html
from signal_matin.source_material import Material
from signal_matin.source_material import _ArticleParser, wikipedia_material
from signal_matin.synthesis import compose_feature
from signal_matin.thought import thought_for_date


BASE = dt.date(2026, 9, 28)  # lundi
WORDS = "Une enquête documentée expose les faits et leurs conséquences avec prudence et précision."


def fixture_feature(category: str, tier: str, count: int) -> FeatureArticle:
    kinds = ["facts", "context", "mechanisms", "analysis", "consequences", "limits"]
    size = 8 if count > 650 else 6
    paragraphs = []
    for index in range(size):
        words = (WORDS.split() * 15)[:count // size]
        paragraphs.append(ArticleParagraph(
            kind="limits" if index == size - 1 else kinds[min(index, 4)],
            text=" ".join(words), source_ids=[1]))
    return FeatureArticle(category=category, title=f"Dossier documenté : {category}",
                          tier=tier, paragraphs=paragraphs,
                          sources=[SourceRef(name="Source de validation", url="https://example.org/original")])


def feed(date: dt.datetime, category: str) -> bytes:
    stamp = date.astimezone(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (f"<rss><channel><item><title>Actualité {category}</title>"
            f"<link>https://example.org/{category}</link>"
            f"<description>Résumé RSS de validation pour {category}.</description>"
            f"<pubDate>{stamp}</pubDate></item></channel></rss>").encode()


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.personal.example.yaml")
        self.now = dt.datetime(2026, 9, 28, 7, tzinfo=dt.timezone(dt.timedelta(hours=10)))

    def test_all_seven_rotations_and_sydney_date(self):
        expected = [
            ["International", "Géopolitique", "Économie"],
            ["Sciences", "Ingénierie", "Intelligence artificielle"],
            ["Philosophie", "Littérature", "Histoire"],
            ["Informatique", "Intelligence artificielle", "Économie"],
            ["Musique", "Culture", "Littérature"],
            ["Histoire", "Sciences", "Curiosités"],
            ["Géopolitique", "Philosophie", "Culture"],
        ]
        for index, categories in enumerate(expected):
            date = BASE + dt.timedelta(days=index)
            self.assertEqual(categories_for_date(self.config, date), categories)
            self.assertLessEqual(len(categories), 3)
        self.assertEqual(local_date(self.config,
                         dt.datetime(2026, 9, 27, 21, tzinfo=dt.timezone.utc)), BASE)
        bad = {**self.config, "editorial": {"rotation": {"monday": ["Culture"]}}}
        with self.assertRaises(ValueError):
            categories_for_date(bad, BASE)

    def test_real_pipeline_uses_only_scheduled_feeds_and_long_writer(self):
        selected = categories_for_date(self.config, BASE)
        urls = {entry["url"]: entry["category"] for entry in self.config["news"]["feeds"]}
        called = []

        def payload(url):
            called.append(url)
            return feed(self.now, urls[url])

        def material(item):
            return Material(item.source, item.title, (item.summary + " ") * 80)

        def chat(messages):
            prompt = messages[-1]["content"]
            target = 800 if "700 à 900" in prompt else 500 if "450 à 600" in prompt else 360
            article = fixture_feature("test", "dossier", target)
            return json.dumps({"title": article.title,
                               "paragraphs": [part.model_dump() for part in article.paragraphs]})

        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
               "SIGNAL_MATIN_LLM_MODEL": "test", "SIGNAL_MATIN_LLM_API_KEY": "test"}
        with patch.dict(os.environ, env), \
             patch("signal_matin.connectors.rss._payload", side_effect=payload), \
             patch("signal_matin.editorial.article_material", side_effect=material), \
             patch("signal_matin.synthesis._chat", side_effect=chat):
            edition = build_live(self.config, now=self.now)
        self.assertEqual([article.category for article in edition.personal_features], selected)
        self.assertTrue(all(urls[url] in selected for url in called))
        self.assertEqual(len(edition.personal_features), 3)
        for article, (low, high) in zip(edition.personal_features,
                                        [(700, 900), (450, 600), (300, 450)]):
            self.assertGreaterEqual(article.word_count(), low)
            self.assertLessEqual(article.word_count(), high)
        self.assertFalse(edition.demo)
        self.assertIsNotNone(edition.thought)
        html = render_html(edition)
        self.assertEqual(html.count('class="personal-feature-head"'), 3)
        self.assertNotIn("Résumé RSS de validation", html)
        self.assertIn('href="https://example.org/International"', html)
        layout = inspecter_html(html)
        self.assertFalse(any(page["overflow"] for page in layout))
        self.assertTrue(all(page["used_ratio"] >= 0.45 for page in layout[:-1]))
        self.assertGreaterEqual(layout[-1]["used_ratio"], 0.25)
        with tempfile.TemporaryDirectory() as temp:
            reader = PdfReader(str(generer_pdf(edition, Path(temp) / "validation.pdf")))
            self.assertGreaterEqual(len(reader.pages), 3)
            self.assertLessEqual(len(reader.pages), 7)
            self.assertTrue(all(abs(float(page.mediabox.height) - 841.89) < 1
                                for page in reader.pages))
            self.assertTrue(any("/Annots" in page for page in reader.pages))
            uris = [str(annotation.get_object().get("/A", {}).get("/URI", ""))
                    for page in reader.pages for annotation in page.get("/Annots", [])]
            self.assertTrue(any("example.org/International" in uri for uri in uris))
            self.assertTrue(all(len(page.extract_text() or "") > 500 for page in reader.pages))

    def test_long_writer_rejects_missing_citations(self):
        source = SourceRef(name="Source", url="https://example.org/a")
        material = Material(source, "Sujet", "Texte de source. " * 100)
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
               "SIGNAL_MATIN_LLM_MODEL": "test", "SIGNAL_MATIN_LLM_API_KEY": "test"}
        invalid = json.dumps({"title": "Sujet", "paragraphs": [
            {"kind": "facts", "text": "Texte sans référence", "source_ids": []},
            {"kind": "limits", "text": "Limite", "source_ids": [1]},
        ]})
        with patch.dict(os.environ, env), patch("signal_matin.synthesis._chat", return_value=invalid):
            self.assertIsNone(compose_feature("Sciences", "dossier", [material], (700, 900)))

    def test_no_sources_never_invents_articles(self):
        with patch("signal_matin.connectors.rss._payload", side_effect=OSError("hors ligne")):
            edition = build_live(self.config, now=self.now)
        self.assertEqual(edition.personal_features, [])
        self.assertIn("Rubriques sans article suffisamment documenté", render_html(edition))
        self.assertNotIn("EDITION DE DEMONSTRATION", render_html(edition))

    def test_rss_without_writer_is_not_printed_as_long_article(self):
        env = {"SIGNAL_MATIN_LLM_URL": "", "SIGNAL_MATIN_LLM_MODEL": "",
               "SIGNAL_MATIN_LLM_API_KEY": ""}
        with patch.dict(os.environ, env), \
             patch("signal_matin.connectors.rss._payload",
                   return_value=feed(self.now, "International")):
            edition = build_live(self.config, now=self.now)
        self.assertTrue(edition.personal_articles)
        self.assertFalse(edition.personal_features)
        self.assertNotIn("Résumé RSS de validation", render_html(edition))

    def test_rss_age_and_quote(self):
        payload = feed(self.now, "Sciences")
        with patch("signal_matin.connectors.rss._payload", return_value=payload):
            items, _ = collect_rss([{"name": "Source", "category": "Sciences",
                                     "url": "https://example.org/rss"}], self.now, require_date=True)
        self.assertEqual(len(items), 1)
        thoughts = [thought_for_date(BASE + dt.timedelta(days=i)) for i in range(7)]
        self.assertEqual(len({thought.text for thought in thoughts}), 7)
        self.assertTrue(all(150 <= len(thought.explanation.split()) <= 200 for thought in thoughts))
        self.assertTrue(all(thought.source_url for thought in thoughts))

    def test_documented_source_material(self):
        parser = _ArticleParser()
        parser.feed("<p>Navigation à ignorer.</p><article><p>" +
                    "Paragraphe public de contexte très suffisamment long pour être retenu. " * 3 +
                    "</p></article>")
        self.assertEqual(len(parser.paragraphs), 1)
        self.assertNotIn("Navigation", parser.paragraphs[0])
        payload = json.dumps({"query": {"pages": {"1": {
            "title": "Post-punk", "extract": "Texte documenté. " * 100
        }}}})
        with patch("signal_matin.source_material._get", return_value=payload):
            material = wikipedia_material("Post-punk")
        self.assertIsNotNone(material)
        self.assertIn("CC BY-SA", material.license_note)
        self.assertIn("wikipedia.org", str(material.source.url))

    def test_sydney_daylight_saving_gate(self):
        summer = dt.datetime(2026, 1, 10, 20, tzinfo=dt.timezone.utc)
        winter = dt.datetime(2026, 7, 10, 21, tzinfo=dt.timezone.utc)
        self.assertTrue(should_run("0 20 * * *", summer))
        self.assertFalse(should_run("0 21 * * *", summer))
        self.assertTrue(should_run("0 21 * * *", winter))
        self.assertFalse(should_run("0 20 * * *", winter))


if __name__ == "__main__":
    unittest.main()
