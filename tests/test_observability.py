"""Diagnostics du journal avec réponses RSS, Tavily et LLM entièrement simulées."""
import datetime as dt
import io
import json
import os
import unittest
from unittest.mock import patch

from signal_matin.config import load_config
from signal_matin.connectors.rss import FeedPayload, collect_rss
from signal_matin.connectors.tavily import SearchTrace, capture_search, search
from signal_matin.editorial import write_features
from signal_matin.models import (ArticleParagraph, EditionMeta, FeatureArticle,
                                 MorningEdition, NewsItem, RubricDiagnostic, SourceRef)
from signal_matin.renderer import _feature_blocks, render_html
from signal_matin.source_material import Material
from signal_matin.synthesis import compose_feature


NOW = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
TITLE = "NASA reporte la mission Artemis III après un incident moteur"
SUMMARY = "La mission Artemis III est reportée après un incident moteur. " * 5
ENV = {"SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
       "SIGNAL_MATIN_LLM_MODEL": "fake-model",
       "SIGNAL_MATIN_LLM_API_KEY": "fake-llm-secret",
       "TAVILY_API_KEY": "fake-tavily-secret"}


def candidate():
    return NewsItem(title=TITLE, category="Ingénierie", summary=SUMMARY,
                    source=SourceRef(name="NASA", title=TITLE,
                                     url="https://nasa.gov/original", published_at=NOW))


def article(category, tier, materials, target):
    paragraphs = [ArticleParagraph(kind=kind, text="Documenté et prudent. " * 35,
                                   source_ids=[1]) for kind in
                  ("facts", "context", "mechanisms", "analysis", "consequences", "limits")]
    return FeatureArticle(category=category, title=TITLE, tier=tier,
                          paragraphs=paragraphs, sources=[materials[0].source])


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.personal.example.yaml")

    def test_rss_logs_http_window_candidate_and_full_rich_length(self):
        text = "Contenu riche documenté. " * 600
        xml = (f'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
               f'<channel><item><title>Cloudflare présente un modèle IA détaillé</title>'
               f'<link>https://blog.cloudflare.com/ia</link>'
               f'<pubDate>Thu, 01 Oct 2026 05:00:00 GMT</pubDate>'
               f'<description>Résumé bref.</description><content:encoded>{text}</content:encoded>'
               f'</item></channel></rss>').encode()
        with patch("signal_matin.connectors.rss._payload",
                   return_value=FeedPayload(xml, 200)), \
             self.assertLogs("signal_matin.connectors.rss", level="INFO") as logs:
            items, _ = collect_rss([{"name": "Cloudflare IA", "category": "Intelligence artificielle",
                                     "url": "https://example.org/feed"}], NOW, require_date=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0].expanded_summary), 11999)
        joined = " ".join(logs.output)
        self.assertIn("HTTP 200", joined)
        self.assertIn("fenêtre temporelle : 1", joined)
        self.assertIn("Cloudflare présente un modèle IA détaillé", joined)
        self.assertIn("contenu RSS=12000 caractères", joined)

    def test_tavily_logs_each_decision_usage_and_run_summary(self):
        results = [
            {"title": "NASA reporte Artemis III après incident moteur : analyse",
             "url": "https://agency.org/artemis", "content": "Artemis III incident moteur. " + "Calendrier examiné. " * 170,
             "published_date": "2026-10-01T05:00:00Z"},
            {"title": "NASA reporte Artemis III après incident moteur : doublon",
             "url": "https://agency.org/artemis?utm_source=x", "content": "Artemis III incident moteur. " + "Calendrier examiné. " * 170,
             "published_date": "2026-10-01T05:00:00Z"},
            {"title": "Actualité sans rapport", "url": "https://other.org/other",
             "content": "Sujet étranger. " * 60, "published_date": "2026-10-01T05:00:00Z"},
            {"title": "URL privée", "url": "http://localhost/private", "content": "Texte"},
        ]
        payload = json.dumps({"results": results, "usage": {"credits": 1}}).encode()
        first = candidate()

        def material(item):
            return Material(item.source, item.title, item.expanded_summary or item.summary)

        rows = []
        with patch.dict(os.environ, ENV), \
             patch("signal_matin.connectors.tavily.urllib.request.urlopen",
                   return_value=io.BytesIO(payload)) as network, \
             patch("signal_matin.editorial.article_material", side_effect=material), \
             patch("signal_matin.editorial.compose_feature", side_effect=article), \
             self.assertLogs("signal_matin.editorial", level="INFO") as logs:
            features = write_features(self.config, NOW.date(), ["Ingénierie"], [first],
                                      diagnostics=rows)
        joined = "\n".join(logs.output)
        self.assertEqual(len(features), 1)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(rows[0].tavily_searches, 1)
        self.assertEqual(rows[0].tavily_credits, 1)
        self.assertEqual(rows[0].result, "published")
        for expected in ("candidat #1", "Matière initiale :", "Recherche : 1/6",
                         "Recherche pour ce candidat : 1/2", "Budget restant : 5",
                         "résultats reçus : 4", "duplicate_url", "irrelevant",
                         "invalid_url", "RETENU", "dossier enrichi", "appel LLM",
                         "Recherches Tavily totales : 1 / 6", "Articles publiés : 1 / 3",
                         "crédits indiqués par l'API : 1"):
            self.assertIn(expected, joined)
        self.assertNotIn(ENV["TAVILY_API_KEY"], joined)
        self.assertNotIn(ENV["SIGNAL_MATIN_LLM_API_KEY"], joined)

    def test_validation_trace_and_distinct_shortfall_notes(self):
        source = SourceRef(name="NASA", url="https://nasa.gov/original")
        material = Material(source, TITLE, "Documenté. " * 120)
        response = {"title": TITLE, "paragraphs": [
            {"kind": kind, "text": "Information sourcée. " * 30, "source_ids": [1]}
            for kind in ("facts", "context", "mechanisms", "analysis", "consequences", "limits")]}
        from signal_matin.synthesis import ComposeTrace, capture_compose
        trace = ComposeTrace()
        with patch.dict(os.environ, ENV), \
             patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace):
            feature = compose_feature("Ingénierie", "dossier", [material], (700, 900))
        self.assertIsNotNone(feature)
        self.assertTrue(trace.response_received)
        self.assertEqual(trace.word_count, feature.word_count())
        self.assertEqual(trace.validation, "OK")
        self.assertEqual(feature.shortfall_reasons, ["article_short"])
        amended = feature.model_copy(update={"shortfall_reasons": ["article_short", "documentation_limited"]})
        markup = _feature_blocks(amended)
        self.assertIn("article plus court que la cible éditoriale", markup)
        self.assertIn("documentation limitée", markup)

    def test_pdf_notice_distinguishes_four_failure_classes(self):
        categories = ["Ingénierie", "Économie", "Culture", "Philosophie"]
        results = ["documentation_insufficient", "tavily_insufficient",
                   "writing_error", "article_rejected"]
        edition = MorningEdition(generated_at=NOW, demo=True, personal_journal=True,
                                 edition=EditionMeta(date=NOW.date(), number=1, title="Diagnostic"),
                                 expected_categories=categories,
                                 personal_diagnostics=[RubricDiagnostic(category=c, result=r)
                                                       for c, r in zip(categories, results)])
        markup = render_html(edition)
        for label in ("documentation insuffisante", "recherche Tavily insuffisante",
                      "erreur de rédaction/API", "article rejeté après validation"):
            self.assertIn(label, markup)

    def test_llm_refusal_is_article_rejected_and_logged_without_secret(self):
        first = candidate()
        material = Material(first.source, first.title, "Documentation détaillée. " * 65)
        rows = []
        with patch.dict(os.environ, ENV), \
             patch("signal_matin.editorial.article_material", return_value=material), \
             patch("signal_matin.synthesis._chat", return_value="INSUFFISANT"), \
             patch("signal_matin.editorial.tavily_search", return_value=[]), \
             self.assertLogs("signal_matin.editorial", level="INFO") as logs:
            features = write_features(self.config, NOW.date(), ["Ingénierie"], [first],
                                      diagnostics=rows)
        self.assertEqual(features, [])
        self.assertEqual(rows[0].result, "article_rejected")
        self.assertEqual(rows[0].reason, "llm_insufficient")
        self.assertTrue(rows[0].llm_called)
        joined = "\n".join(logs.output)
        self.assertIn("réponse reçue : OUI", joined)
        self.assertIn("validation : REJET", joined)
        self.assertNotIn(ENV["SIGNAL_MATIN_LLM_API_KEY"], joined)


if __name__ == "__main__":
    unittest.main()
