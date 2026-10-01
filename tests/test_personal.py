"""Rotation, rédaction et pagination sans réseau ni courrier sortant."""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from pypdf import PdfReader

from scripts.should_run_sydney import should_run
from signal_matin.config import load_config
from signal_matin.connectors.rss import collect_rss
from signal_matin.editorial import _related, categories_for_date, local_date, write_features
from signal_matin.models import ArticleParagraph, FeatureArticle, NewsItem, SourceRef
from signal_matin.pdf import generer_pdf, inspecter_html
from signal_matin.pipeline import build_live
from signal_matin.renderer import render_html
from signal_matin.source_material import Material
from signal_matin.source_material import _ArticleParser, wikipedia_material
from signal_matin.synthesis import _chat, compose_feature
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
    return (f"<rss><channel><item><title>Actualité {category.replace('&', '&amp;')}</title>"
            f"<link>https://example.org/{quote(category)}</link>"
            f"<description>Résumé RSS de validation pour {category.replace('&', '&amp;')}.</description>"
            f"<pubDate>{stamp}</pubDate></item></channel></rss>").encode()


class PersonalTests(unittest.TestCase):
    def setUp(self):
        no_paid_search = patch.dict(os.environ, {"TAVILY_API_KEY": ""})
        no_paid_search.start()
        self.addCleanup(no_paid_search.stop)
        self.config = load_config("config.personal.example.yaml")
        self.now = dt.datetime(2026, 9, 28, 7, tzinfo=dt.timezone(dt.timedelta(hours=10)))

    def test_all_seven_rotations_and_sydney_date(self):
        expected = [
            ["International & Géopolitique", "Économie", "Sciences & Curiosités"],
            ["Sciences & Curiosités", "Ingénierie", "Intelligence artificielle"],
            ["Philosophie", "Littérature", "Histoire"],
            ["Informatique", "Intelligence artificielle", "Économie"],
            ["Musique", "Culture", "Littérature"],
            ["Histoire", "Sciences & Curiosités", "Culture"],
            ["International & Géopolitique", "Philosophie", "Culture"],
        ]
        for index, categories in enumerate(expected):
            date = BASE + dt.timedelta(days=index)
            self.assertEqual(categories_for_date(self.config, date), categories)
            self.assertLessEqual(len(categories), 3)
        self.assertEqual(len({name for day in expected for name in day}), 11)
        self.assertEqual(local_date(self.config,
                         dt.datetime(2026, 9, 27, 21, tzinfo=dt.timezone.utc)), BASE)
        bad = {**self.config, "editorial": {"rotation": {"monday": ["Culture"]}}}
        with self.assertRaises(ValueError):
            categories_for_date(bad, BASE)

    def test_real_pipeline_uses_only_scheduled_feeds_and_long_writer(self):
        selected = categories_for_date(self.config, BASE)
        urls: dict[str, set[str]] = {}
        for entry in self.config["news"]["feeds"]:
            urls.setdefault(entry["url"], set()).add(entry["category"])
        called = []

        def payload(url):
            called.append(url)
            return feed(self.now, next(iter(urls[url] & set(selected))))

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
        self.assertTrue(all(urls[url] & set(selected) for url in called))
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
        self.assertIn('href="https://example.org/International%20%26%20G%C3%A9opolitique"', html)
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
            self.assertTrue(any("example.org/International%20%26%20G%C3%A9opolitique" in uri for uri in uris))
            self.assertTrue(all(len(page.extract_text() or "") > 500 for page in reader.pages))

    def test_long_writer_rejects_missing_citations(self):
        source = SourceRef(name="Source", url="https://example.org/a")
        material = Material(source, "Sujet", "Texte de source. " * 100)
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
               "SIGNAL_MATIN_LLM_MODEL": "test", "SIGNAL_MATIN_LLM_API_KEY": "test"}
        invalid = json.dumps({"title": "Sujet", "paragraphs": [
            {"kind": kind, "text": "Texte sans référence", "source_ids": [] if kind == "facts" else [1]}
            for kind in ("facts", "context", "mechanisms", "analysis", "consequences", "limits")
        ]})
        with patch.dict(os.environ, env), patch("signal_matin.synthesis._chat", return_value=invalid), \
             self.assertLogs("signal_matin.synthesis", level="WARNING") as logs:
            self.assertIsNone(compose_feature("Sciences", "dossier", [material], (700, 900)))
        self.assertIn("référence de source absente", " ".join(logs.output))

    def test_llm_settings_are_used_in_chat_request(self):
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/v1/chat/completions",
               "SIGNAL_MATIN_LLM_MODEL": "test-model", "SIGNAL_MATIN_LLM_API_KEY": "dummy-key"}

        def respond(request, timeout):
            self.assertEqual(request.full_url, env["SIGNAL_MATIN_LLM_URL"])
            self.assertEqual(request.get_header("Authorization"), "Bearer dummy-key")
            self.assertEqual(json.loads(request.data), {
                "model": "test-model", "temperature": 0,
                "messages": [{"role": "user", "content": "test"}],
            })
            self.assertEqual(timeout, 90)
            return io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}]}')

        with patch.dict(os.environ, env), \
             patch("signal_matin.synthesis.urllib.request.urlopen", side_effect=respond):
            self.assertEqual(_chat([{"role": "user", "content": "test"}]), "OK")

    def test_client_challenge_prevents_ai_call_and_explains_each_category(self):
        selected = categories_for_date(self.config, dt.date(2026, 10, 1))
        items = [NewsItem(title="Sujet", category=category, summary="Résumé RSS bref.",
                          source=SourceRef(name="Source", url="https://example.org/article"))
                 for category in selected]
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/v1/chat/completions",
               "SIGNAL_MATIN_LLM_MODEL": "test-model", "SIGNAL_MATIN_LLM_API_KEY": "dummy-key"}
        with patch.dict(os.environ, env), \
             patch("signal_matin.source_material._robots_allow", return_value=True), \
             patch("signal_matin.source_material._get",
                   return_value="<html><title>Client Challenge</title></html>"), \
             patch("signal_matin.editorial.compose_feature") as compose, \
             self.assertLogs("signal_matin", level="WARNING") as logs:
            self.assertEqual(write_features(self.config, dt.date(2026, 10, 1), selected, items), [])
        compose.assert_not_called()
        self.assertEqual(sum("aucun appel IA" in line for line in logs.output), 3)
        self.assertEqual(sum("page de vérification" in line for line in logs.output), 1)

    def test_ai_http_error_reports_status_without_secret(self):
        source = SourceRef(name="Source", url="https://example.org/article")
        item = NewsItem(title="Sujet", category="Informatique", summary="Résumé", source=source)
        material = Material(source, "Sujet", "Texte documenté. " * 100)
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/v1/chat/completions",
               "SIGNAL_MATIN_LLM_MODEL": "test-model", "SIGNAL_MATIN_LLM_API_KEY": "dummy-key"}
        error = urllib.error.HTTPError(env["SIGNAL_MATIN_LLM_URL"], 401, "Unauthorized",
                                       {"x-request-id": "req-test"}, None)
        with patch.dict(os.environ, env), \
             patch("signal_matin.editorial.article_material", return_value=material), \
             patch("signal_matin.editorial.compose_feature", side_effect=error), \
             self.assertLogs("signal_matin.editorial", level="WARNING") as logs:
            self.assertEqual(write_features(self.config, dt.date(2026, 10, 1),
                                            ["Informatique"], [item]), [])
        self.assertIn("HTTP 401, request_id=req-test", " ".join(logs.output))
        self.assertNotIn("dummy-key", " ".join(logs.output))

    def test_selected_source_is_not_counted_twice(self):
        source = SourceRef(name="Source", url="https://example.org/article")
        first = NewsItem(title="Nouvelles puces informatiques", category="Informatique",
                         summary="Bref.", source=source)
        chosen = NewsItem(title="Nouvelles puces informatiques pour ordinateurs",
                          category="Informatique", summary="Détail. " * 35, source=source)
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/v1/chat/completions",
               "SIGNAL_MATIN_LLM_MODEL": "test-model", "SIGNAL_MATIN_LLM_API_KEY": "dummy-key"}

        def material(item):
            return Material(item.source, item.title, "Texte documenté. " * 100)

        with patch.dict(os.environ, env), \
             patch("signal_matin.editorial.article_material", side_effect=material) as extract, \
             patch("signal_matin.editorial.compose_feature", return_value=None):
            write_features(self.config, dt.date(2026, 10, 1), ["Informatique"],
                           [first, chosen])
        self.assertEqual([call.args[0] for call in extract.call_args_list], [chosen])

    def test_multisource_group_uses_event_evidence_not_only_shared_organisation(self):
        published = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
        def item(name, url, title, summary):
            return NewsItem(title=title, category="Ingénierie", summary=summary,
                            source=SourceRef(name=name, title=title, url=url, published_at=published))

        first = item("NASA", "https://nasa.gov/a",
                     "NASA reporte Artemis III après un incident de moteur",
                     "La mission Artemis III est reportée après un incident sur le moteur du véhicule.")
        second = item("Smithsonian", "https://smithsonianmag.com/b",
                      "Un incident moteur décale le calendrier Artemis III",
                      "Artemis III connaît un report à cause d'un incident sur le moteur du véhicule.")
        other = item("NASA", "https://nasa.gov/c",
                     "NASA teste un moteur pour Artemis II",
                     "Un essai de moteur distinct est prévu pour le programme Artemis II.")
        self.assertTrue(_related(first, second))
        self.assertFalse(_related(first, other))
        self.assertFalse(_related(second, other))
        doi_a = item("Revue A", "https://a.example/article",
                     "Étude sur un sujet précis", "DOI 10.1234/exemple.2026")
        doi_b = item("Revue B", "https://b.example/article",
                     "Résultats publiés cette semaine", "DOI 10.1234/exemple.2026")
        self.assertTrue(_related(doi_a, doi_b))
        self.assertFalse(_related(doi_a, doi_a))
        older = doi_b.model_copy(update={"source": doi_b.source.model_copy(update={
            "published_at": published - dt.timedelta(days=5)})})
        self.assertFalse(_related(doi_a, older))

        seen = []
        def compose(category, tier, materials, target):
            seen.append([material.source.name for material in materials])
            return fixture_feature(category, tier, 800)

        with patch("signal_matin.editorial.llm_configured", return_value=True), \
             patch("signal_matin.editorial.article_material",
                   side_effect=lambda news: Material(news.source, news.title, news.summary * 12)), \
             patch("signal_matin.editorial.compose_feature", side_effect=compose):
            features = write_features(self.config, published.date(), ["Ingénierie"],
                                      [first, second, other])
        self.assertEqual(len(features), 1)
        self.assertEqual(set(seen[0]), {"NASA", "Smithsonian"})

    def test_failed_rubric_moves_to_next_and_reuses_first_tier(self):
        items = [NewsItem(title="Une étude informatique documentée", category="Informatique",
                          summary="Texte. " * 150,
                          source=SourceRef(name="GitHub Blog", url="https://github.blog/a")),
                 NewsItem(title="Un travail sur les réseaux de neurones", category="Intelligence artificielle",
                          summary="Texte. " * 150,
                          source=SourceRef(name="arXiv", url="https://arxiv.org/abs/1"))]
        with patch("signal_matin.editorial.llm_configured", return_value=True), \
             patch("signal_matin.editorial.article_material",
                   side_effect=lambda item: Material(item.source, item.title, item.summary)), \
             patch("signal_matin.editorial.compose_feature",
                   side_effect=lambda category, tier, materials, target: fixture_feature(category, tier, 800)):
            features = write_features(self.config, BASE,
                                      ["Philosophie", "Informatique", "Intelligence artificielle"], items)
        self.assertEqual([(feature.category, feature.tier) for feature in features],
                         [("Informatique", "dossier"), ("Intelligence artificielle", "article")])

    def test_pdf_lists_only_cited_sources_with_title_date_and_link(self):
        when = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
        first = Material(SourceRef(name="NASA", title="Une découverte vérifiée",
                                   url="https://nasa.gov/actualite", published_at=when),
                         "Une découverte vérifiée", "Matière vérifiée. " * 100)
        unused = Material(SourceRef(name="Autre média", title="Contexte non utilisé",
                                    url="https://example.org/non-utilise", published_at=when),
                          "Contexte non utilisé", "Autre matière. " * 100)
        fixture = fixture_feature("Sciences & Curiosités", "lecture", 360)
        response = json.dumps({"title": fixture.title,
                               "paragraphs": [paragraph.model_dump() for paragraph in fixture.paragraphs]})
        env = {"SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
               "SIGNAL_MATIN_LLM_MODEL": "test", "SIGNAL_MATIN_LLM_API_KEY": "test"}
        with patch.dict(os.environ, env), patch("signal_matin.synthesis._chat", return_value=response):
            feature = compose_feature("Sciences & Curiosités", "lecture", [first, unused], (300, 450))
        self.assertIsNotNone(feature)
        self.assertEqual([source.name for source in feature.sources], ["NASA"])
        second_response = json.loads(response)
        for paragraph in second_response["paragraphs"]:
            paragraph["source_ids"] = [2]
        with patch.dict(os.environ, env), \
             patch("signal_matin.synthesis._chat", return_value=json.dumps(second_response)):
            second_feature = compose_feature("Sciences & Curiosités", "lecture", [first, unused], (300, 450))
        self.assertEqual([source.name for source in second_feature.sources], ["Autre média"])
        self.assertTrue(all(paragraph.source_ids == [1] for paragraph in second_feature.paragraphs))
        # La liste HTML est celle utilisée par le PDF.
        from signal_matin.renderer import _feature_blocks
        html = _feature_blocks(feature)
        self.assertIn("Une découverte vérifiée", html)
        self.assertIn("01/10/2026", html)
        self.assertIn('href="https://nasa.gov/actualite"', html)
        self.assertNotIn("Contexte non utilisé", html)
        with patch("signal_matin.connectors.rss._payload", side_effect=OSError("hors ligne")), \
             patch("signal_matin.editorial.llm_configured", return_value=False):
            edition = build_live(self.config, now=self.now)
        edition = edition.model_copy(update={"personal_features": [feature]})
        with tempfile.TemporaryDirectory() as temp:
            reader = PdfReader(str(generer_pdf(edition, Path(temp) / "sources.pdf")))
            pdf_text = " ".join(page.extract_text() or "" for page in reader.pages)
            urls = [str(annotation.get_object().get("/A", {}).get("/URI", ""))
                    for page in reader.pages for annotation in page.get("/Annots", [])]
        self.assertIn("Une découverte vérifiée", pdf_text)
        self.assertIn("01/10/2026", pdf_text)
        self.assertIn("https://nasa.gov/actualite", urls)
        self.assertNotIn("Contexte non utilisé", pdf_text)

    def test_no_sources_never_invents_articles(self):
        with patch("signal_matin.connectors.rss._payload", side_effect=OSError("hors ligne")), \
             self.assertLogs("signal_matin.connectors.rss", level="WARNING") as logs:
            edition = build_live(self.config, now=self.now)
        self.assertIn("flux RSS impossible (OSError", " ".join(logs.output))
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
