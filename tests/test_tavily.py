"""Recherche documentaire simulée : aucun appel réseau, LLM ou courrier réel."""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader
from signal_matin.config import load_config
from signal_matin.connectors.tavily import SearchHit, TavilyError, search
from signal_matin.editorial import write_features
from signal_matin.models import (ArticleParagraph, DataSourceStatus, DataState,
                                 EditionMeta, FeatureArticle, MorningEdition, NewsItem, SourceRef)
from signal_matin.pdf import generer_pdf
from signal_matin.pipeline import build_live
from signal_matin.renderer import _feature_blocks
from signal_matin.source_material import Material
from signal_matin.synthesis import compose_feature

DATE = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
TITLE = "NASA reporte la mission Artemis III après un incident moteur"
SUMMARY = ("La mission Artemis III est reportée après un incident moteur. "
           "La NASA confirme que le calendrier technique et les essais seront réexaminés. " * 4)[:310]


def item(category="Ingénierie", *, title=TITLE, summary=SUMMARY, name="NASA", url="https://nasa.gov/initial"):
    return NewsItem(title=title, category=category, summary=summary[:1600],
                    expanded_summary=summary if len(summary) > 1600 else "",
                    source=SourceRef(name=name, title=title, url=url, published_at=DATE))


def hit(name, slug, text):
    return SearchHit(title=f"NASA reporte Artemis III après incident moteur : {name}",
                     url=f"https://{slug}.org/artemis-iii", content=text,
                     publisher=f"{slug}.org", published_at=DATE, score=0.9)


def feature(category, tier, materials, target):
    paragraphs = [ArticleParagraph(kind=kind, text="Analyse documentée et limitée. " * 40,
                                   source_ids=[1]) for kind in
                  ("facts", "context", "mechanisms", "analysis", "consequences", "limits")]
    return FeatureArticle(category=category, title=TITLE, tier=tier, paragraphs=paragraphs,
                          sources=[materials[0].source])


class TavilyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.personal.example.yaml")
        self.config["editorial"]["v2"]["enabled"] = False
        self.config["tavily"]["max_searches_per_edition"] = 6
        self.env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
                    "SIGNAL_MATIN_LLM_MODEL": "fake",
                    "SIGNAL_MATIN_LLM_API_KEY": "fake-secret",
                    "TAVILY_API_KEY": "fake-tavily-secret"}

    @staticmethod
    def material(news):
        return Material(news.source, news.title, news.expanded_summary or news.summary)

    def test_a_short_rss_search_enriches_before_writer(self):
        sources = [hit("NASA", "primary", "Artemis III incident moteur. " + "NASA détaille les essais. " * 85),
                   hit("Agence", "agency", "Artemis III incident moteur. " + "Le calendrier reste incertain. " * 85)]
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.tavily_search", return_value=sources) as tavily, \
             patch("signal_matin.editorial.compose_feature", side_effect=feature) as writer:
            result = write_features(self.config, DATE.date(), ["Ingénierie"], [item()])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(item().summary), 310)
        self.assertGreaterEqual(sum(len(m.text) for m in writer.call_args.args[2]), 3000)
        self.assertEqual(len(writer.call_args.args[2]), 3)
        self.assertGreaterEqual(tavily.call_count, 1)

    def test_b_poor_search_still_blocks_writer(self):
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.tavily_search", return_value=[hit("Bref", "brief", "Artemis III incident moteur. " * 5)]), \
             patch("signal_matin.editorial.compose_feature") as writer:
            result = write_features(self.config, DATE.date(), ["Ingénierie"], [item()])
        self.assertEqual(result, [])
        writer.assert_not_called()

    def test_c_existing_rich_multisource_skips_tavily(self):
        first = item(summary="Essais détaillés. " * 125)
        second = item(title="Incident moteur : NASA reporte Artemis III",
                      summary="Calendrier analysé. " * 125, name="Agence",
                      url="https://agency.org/second")
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.tavily_search") as tavily, \
             patch("signal_matin.editorial.compose_feature", side_effect=feature):
            result = write_features(self.config, DATE.date(), ["Ingénierie"], [first, second])
        self.assertEqual(len(result), 1)
        tavily.assert_not_called()

    def test_one_rich_source_can_publish_with_limitation_note(self):
        source = item(summary="Documentation technique originale. " * 150)
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.tavily_search", return_value=[]), \
             patch("signal_matin.editorial.compose_feature", side_effect=feature):
            result = write_features(self.config, DATE.date(), ["Ingénierie"], [source])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].shortfall)

    def test_d_duplicate_url_and_content_are_not_counted_twice(self):
        text = "Artemis III incident moteur. " + "NASA détaille les essais. " * 85
        hits = [hit("Original", "primary", text),
                SearchHit("NASA reporte Artemis III après incident moteur", "https://primary.org/artemis-iii?utm_source=x",
                          text, "primary.org", DATE),
                hit("Copie", "copy", text),
                hit("Indépendant", "independent", "Artemis III incident moteur. " + "Le calendrier demeure incertain. " * 85)]
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.tavily_search", return_value=hits), \
             patch("signal_matin.editorial.compose_feature", side_effect=feature) as writer:
            write_features(self.config, DATE.date(), ["Ingénierie"], [item()])
        self.assertEqual({str(m.source.url) for m in writer.call_args.args[2]},
                         {"https://nasa.gov/initial", "https://primary.org/artemis-iii", "https://independent.org/artemis-iii"})

    def test_e_edition_budget_stops_additional_searches(self):
        self.config["tavily"]["max_searches_per_edition"] = 1
        second = item(category="Informatique", title="GitHub corrige une faille dans son service GitHub Actions",
                      summary="GitHub décrit une faille et les mesures de correction. " * 5,
                      url="https://github.blog/faille")
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.tavily_search", return_value=[]) as tavily, \
             patch("signal_matin.editorial.compose_feature") as writer:
            result = write_features(self.config, DATE.date(), ["Ingénierie", "Informatique"], [item(), second])
        self.assertEqual(result, [])
        self.assertEqual(tavily.call_count, 1)
        writer.assert_not_called()

    def test_f_pipeline_loads_fallback_only_after_primary_fails(self):
        self.config["tavily"]["max_searches_per_edition"] = 0
        now = dt.datetime(2026, 9, 28, 7, tzinfo=dt.timezone(dt.timedelta(hours=10)))
        calls = []
        def collect(feeds, now, **kwargs):
            categories = {feed["category"] for feed in feeds}
            calls.append(categories)
            if categories == {"Ingénierie"}:
                found = [item(category="Ingénierie", summary="Document technique détaillé. " * 80)]
            else:
                found = []
            return found, DataSourceStatus(name="RSS", state=DataState.LIVE, item_count=len(found))
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.pipeline.collect_rss", side_effect=collect), \
             patch("signal_matin.editorial.article_material", side_effect=self.material), \
             patch("signal_matin.editorial.compose_feature", side_effect=feature):
            edition = build_live(self.config, now=now)
        self.assertEqual(edition.personal_features[0].category, "Ingénierie")
        self.assertEqual(edition.personal_features[0].tier, "dossier")
        self.assertEqual(calls[0], set(self.config["editorial"]["rotation"]["monday"]))
        self.assertIn({"Ingénierie"}, calls)

    def test_g_only_cited_tavily_sources_appear_in_pdf_markup(self):
        materials = [Material(SourceRef(name="NASA", title="Original", url="https://nasa.gov/a"),
                              "Original", "Information vérifiée. " * 90),
                     Material(SourceRef(name="agency.org", title="Recherche Tavily",
                                        url="https://agency.org/a"), "Recherche Tavily",
                              "Autre information. " * 90, origin="tavily")]
        response = {"title": TITLE, "paragraphs": [
            {"kind": kind, "text": "Information sourcée et prudente. " * 15, "source_ids": [2]}
            for kind in ("facts", "context", "mechanisms", "analysis", "consequences", "limits")]}
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.synthesis._chat", return_value=json.dumps(response)):
            article = compose_feature("Ingénierie", "lecture", materials, (300, 450))
        self.assertIsNotNone(article)
        markup = _feature_blocks(article)
        self.assertIn("https://agency.org/a", markup)
        self.assertNotIn("https://nasa.gov/a", markup)
        self.assertEqual([source.name for source in article.sources], ["agency.org"])
        edition = MorningEdition(generated_at=DATE, demo=True, personal_journal=True,
                                 edition=EditionMeta(date=DATE.date(), number=1,
                                                     title="Simulation technique"),
                                 personal_features=[article])
        with tempfile.TemporaryDirectory() as directory:
            reader = PdfReader(str(generer_pdf(edition, Path(directory) / "simulation.pdf")))
            urls = [str(annotation.get_object().get("/A", {}).get("/URI", ""))
                    for page in reader.pages for annotation in page.get("/Annots", [])]
        self.assertIn("https://agency.org/a", urls)
        self.assertNotIn("https://nasa.gov/a", urls)

    def test_connector_uses_official_post_without_exposing_key(self):
        def respond(request, timeout):
            self.assertEqual(request.full_url, "https://api.tavily.com/search")
            self.assertEqual(request.get_header("Authorization"), "Bearer fake-tavily-secret")
            self.assertEqual(timeout, 25)
            payload = json.loads(request.data)
            self.assertEqual(payload["topic"], "news")
            self.assertEqual(payload["time_range"], "week")
            self.assertFalse(payload["include_answer"])
            self.assertFalse(payload["include_raw_content"])
            return io.BytesIO(json.dumps({"results": [{"title": TITLE, "url": "https://nasa.gov/a",
                                                  "content": "Texte", "published_date": "2026-10-01T05:00:00Z"}]}).encode())
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.connectors.tavily.urllib.request.urlopen", side_effect=respond):
            result = search("Artemis III", recent=True)
        self.assertEqual(result[0].publisher, "nasa.gov")
        self.assertEqual(result[0].published_at, DATE)
        with patch.dict(os.environ, self.env), \
             patch("signal_matin.connectors.tavily.urllib.request.urlopen", side_effect=OSError("fake-tavily-secret")):
            with self.assertRaises(TavilyError) as error:
                search("Artemis III")
        self.assertNotIn("fake-tavily-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
