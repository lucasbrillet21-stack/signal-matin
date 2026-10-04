"""Pipeline V2 simulé : aucune requête réseau ni aucun courrier."""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

from signal_matin.api_cost import UsageCounter, capture_usage, estimate, record_llm_usage
from signal_matin.config import load_config
from signal_matin.connectors.tavily import SearchHit, TavilyError
from signal_matin.editorial import categories_for_date, write_features
from signal_matin.editorial_v2 import ResearchBudget, _research
from signal_matin.models import (ApiCost, ArticleParagraph, EditionMeta, FeatureArticle,
                                 MorningEdition, NewsItem, RubricDiagnostic, SourceRef,
                                 DataSourceStatus, DataState)
from signal_matin.pdf import generer_pdf
from signal_matin.pipeline import build_live
from signal_matin.source_material import Material
from signal_matin.timeless import remember_published, topic_for_date


DATE = dt.date(2026, 10, 1)
WHEN = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
TITLE = "NASA reporte la mission Artemis III après un incident moteur"


def item():
    return NewsItem(title=TITLE, category="Ingénierie",
                    summary="NASA reporte Artemis III après un incident moteur. " * 5,
                    source=SourceRef(name="NASA", title=TITLE,
                                     url="https://nasa.gov/initial", published_at=WHEN))


def hit(index: int, length: int = 1600):
    text = (f"Artemis III incident moteur. Analyse {index} du calendrier, des essais et "
            f"des conséquences. " * 30)
    return SearchHit(title=f"NASA reporte Artemis III après incident moteur : rapport {index}",
                     url=f"https://source{index}.org/artemis", publisher=f"source{index}.org",
                     content=(text * 10)[:length], published_at=WHEN)


def article_json(marker: str):
    kinds = ("facts", "context", "mechanisms", "analysis", "consequences", "limits")
    return json.dumps({"title": f"{TITLE} — {marker}", "paragraphs": [
        {"kind": kind, "heading": ["Contexte et faits", "Mécanismes et enjeux",
                                      "Conséquences et limites"][index // 2] if index % 2 == 0 else "",
         "text": (f"{marker} information documentée propre au point {index}. " * 14),
         "source_ids": [1]} for index, kind in enumerate(kinds)]}, ensure_ascii=False)


def critique_json(research=False, queries=None):
    return json.dumps({"repetition": 0.1, "continuity": 0.8, "clarity": 0.8,
                       "depth": 0.7, "source_diversity": 0.7,
                       "unsupported_claims": [], "repeated_points": [],
                       "missing_context": ["chronologie"] if research else [],
                       "missing_questions": [], "weak_passages": [],
                       "additional_research_needed": research,
                       "suggested_search_queries": queries or []})


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.personal.example.yaml")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "SIGNAL_MATIN_LLM_URL": "https://example.org/chat",
            "SIGNAL_MATIN_LLM_MODEL": "gpt-4.1-mini",
            "SIGNAL_MATIN_LLM_API_KEY": "secret-llm-v2",
            "TAVILY_API_KEY": "secret-tavily-v2",
            "SIGNAL_MATIN_TAVILY_LEDGER": str(Path(self.temp.name) / "ledger.json"),
            "SIGNAL_MATIN_TOPIC_HISTORY": str(Path(self.temp.name) / "topics.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_pre_draft_stops_early_when_complete(self):
        from signal_matin.editorial_v2 import _angles
        first = item()
        material = Material(first.source, first.title, first.summary)
        materials = [material]
        budget = ResearchBudget(self.config, DATE)
        rows = RubricDiagnostic(category="Ingénierie")
        varied = [hit(1, 2600), hit(2, 2600), hit(3, 2600)]
        def material_for(news):
            return Material(news.source, news.title, news.expanded_summary or news.summary)
        with patch("signal_matin.editorial_v2.tavily_search", return_value=varied) as tavily, \
             patch("signal_matin.editorial.article_material", side_effect=material_for):
            _research(first, materials, self.config, budget, rows, {}, "dossier",
                      "pre-draft", _angles(first), 10, 3)
        self.assertEqual(tavily.call_count, 3)
        self.assertGreaterEqual(len(materials), 3)

    def test_poor_documentation_tries_at_most_ten_angles_and_no_llm(self):
        first = item()
        rows = []
        def material_for(news):
            return Material(news.source, news.title, news.summary)
        with patch("signal_matin.editorial_v2.article_material", side_effect=material_for), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily, \
             patch("signal_matin.editorial_v2.compose_feature") as writer:
            features = write_features(self.config, DATE, ["Ingénierie"], [first], diagnostics=rows)
        self.assertEqual(features, [])
        self.assertEqual(tavily.call_count, 10)
        self.assertEqual(rows[0].tavily_searches, 10)
        writer.assert_not_called()

    def test_critique_without_more_research_and_real_rewrite(self):
        first = item()
        rich = Material(first.source, first.title, "Document technique vérifié. " * 150)
        other = Material(SourceRef(name="Agence", url="https://agency.org/report"),
                         TITLE, "Contexte indépendant détaillé. " * 100)
        second = first.model_copy(update={"source": other.source})
        output = [article_json("draft"), critique_json(), article_json("final")]
        rows, costs = [], []
        with patch("signal_matin.editorial_v2.article_material",
                   side_effect=lambda news: rich if news.source.name == "NASA" else other), \
             patch("signal_matin.synthesis._chat", side_effect=output) as chat, \
             patch("signal_matin.editorial_v2.tavily_search") as tavily:
            features = write_features(self.config, DATE, ["Ingénierie"], [first, second],
                                      diagnostics=rows, costs=costs)
        self.assertEqual(chat.call_count, 3)
        tavily.assert_not_called()
        self.assertEqual(len(features), 1)
        self.assertIn("final", features[0].title)
        self.assertIn("draft", chat.call_args.args[0][-1]["content"])
        self.assertIn("CRITIQUE:", chat.call_args.args[0][-1]["content"])
        self.assertEqual(rows[0].result, "published")
        self.assertIsNone(costs[0].total_usd)  # mock sans usage API

    def test_post_critique_uses_only_five_new_queries(self):
        first = item()
        rich = Material(first.source, first.title, "Document technique détaillé. " * 160)
        other = Material(SourceRef(name="Agence", url="https://agency.org/report"),
                         TITLE, "Autre contexte et chiffres. " * 130)
        second = first.model_copy(update={"source": other.source})
        queries = [f"{TITLE} lacune indépendante {index}" for index in range(7)]
        output = [article_json("draft"), critique_json(True, queries), article_json("final")]
        with patch("signal_matin.editorial_v2.article_material",
                   side_effect=lambda news: rich if news.source.name == "NASA" else other), \
             patch("signal_matin.synthesis._chat", side_effect=output), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily:
            features = write_features(self.config, DATE, ["Ingénierie"], [first, second])
        self.assertEqual(len(features), 1)
        self.assertEqual(tavily.call_count, 5)
        self.assertEqual(len({call.args[0] for call in tavily.call_args_list}), 5)

    def test_post_critique_never_repeats_pre_draft_query(self):
        first = item()
        budget = ResearchBudget(self.config, DATE)
        row = RubricDiagnostic(category="Ingénierie")
        seen = set()
        materials = []
        initial = " ".join(first.title.casefold().split())
        with patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily:
            _research(first, materials, self.config, budget, row, {}, "dossier", "pre-draft",
                      [(first.title, "principal")], 1, 3, seen)
            _research(first, materials, self.config, budget, row, {}, "dossier", "post-critique",
                      [(first.title, "répétition"), (first.title + " contexte", "lacune")],
                      5, seen_queries=seen)
        self.assertIn(initial, seen)
        self.assertEqual(tavily.call_count, 2)
        self.assertEqual(tavily.call_args_list[-1].args[0], first.title + " contexte")

    def test_duplicate_headings_rejected_and_cost_calculated(self):
        from signal_matin.synthesis import compose_feature
        first = item()
        material = Material(first.source, TITLE, "Documentation vérifiée. " * 100)
        response = json.loads(article_json("draft"))
        response["paragraphs"][2]["heading"] = response["paragraphs"][0]["heading"]
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)):
            self.assertIsNone(compose_feature("Ingénierie", "dossier", [material], (700, 900)))
        usage = UsageCounter()
        with capture_usage(usage):
            record_llm_usage({"prompt_tokens": 1000, "completion_tokens": 500})
        cost = estimate(self.config, usage, 3, 3)
        self.assertEqual(cost.llm_calls, 1)
        self.assertAlmostEqual(cost.llm_usd, 0.0012)
        self.assertAlmostEqual(cost.tavily_usd, 0.024)
        self.assertAlmostEqual(cost.total_usd, 0.0252)

    def test_chat_completions_usage_is_counted_from_response(self):
        from signal_matin.synthesis import _chat
        response = {"choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 45}}
        counter = UsageCounter()
        with capture_usage(counter), \
             patch("signal_matin.synthesis.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(response).encode())):
            self.assertEqual(_chat([{"role": "user", "content": "test"}]), "OK")
        self.assertEqual((counter.llm_calls, counter.input_tokens, counter.output_tokens),
                         (1, 123, 45))

    def test_timeless_topics_and_recent_history(self):
        self.assertEqual(categories_for_date(self.config, dt.date(2026, 10, 4))[-1],
                         "Mythologies & Religions")
        history = topic_for_date(self.config, "Histoire", DATE)
        myth = topic_for_date(self.config, "Mythologies & Religions", DATE)
        self.assertIsNotNone(history)
        self.assertIsNotNone(myth)
        self.assertIsNone(history.source.published_at)
        self.assertIsNone(history.source.url)
        self.assertIn("source factuelle", history.summary)
        remember_published("Histoire", history.title, DATE)
        later = topic_for_date(self.config, "Histoire", DATE)
        self.assertNotEqual(later.title, history.title)

    def test_history_bypasses_rss_window_and_starts_with_topic(self):
        self.config["tavily"]["max_pre_draft_searches"] = 1
        expected = topic_for_date(self.config, "Histoire", DATE)
        rows = []
        with patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily, \
             patch("signal_matin.editorial_v2.article_material") as extract, \
             patch("signal_matin.editorial_v2.compose_feature") as writer:
            features = write_features(self.config, DATE, ["Histoire"], [], diagnostics=rows)
        self.assertEqual(features, [])
        self.assertEqual(tavily.call_count, 1)
        self.assertIn(expected.title, tavily.call_args.args[0])
        extract.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(rows[0].candidates_tried, 1)

    def test_pipeline_uses_history_bank_without_current_history_rss(self):
        day = dt.date(2026, 9, 30)  # mercredi
        now = dt.datetime(2026, 9, 30, 7, tzinfo=dt.timezone(dt.timedelta(hours=10)))
        self.config["tavily"]["max_pre_draft_searches"] = 1
        def empty_rss(feeds, when, **kwargs):
            self.assertFalse(any(feed.get("category") == "Histoire" for feed in feeds))
            return [], DataSourceStatus(name="RSS simulé", state=DataState.UNAVAILABLE)
        with patch("signal_matin.pipeline.collect_rss", side_effect=empty_rss), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]):
            edition = build_live(self.config, now=now)
        self.assertIn("Histoire", edition.expected_categories)
        history = next(row for row in edition.personal_diagnostics if row.category == "Histoire")
        self.assertEqual(history.candidates_tried, 1)
        self.assertEqual(history.tavily_searches, 1)

    def test_mythology_short_title_accepts_documented_matching_source(self):
        from signal_matin.editorial_v2 import _angles
        first = NewsItem(title="Prométhée et le feu", category="Mythologies & Religions",
                         summary="Sujet intemporel : Prométhée et le feu dans les sources grecques. " * 3,
                         source=SourceRef(name="Banque de sujets"))
        search_hit = SearchHit(title="Le mythe de Prométhée et du feu",
                               url="https://museum.org/promethee", publisher="museum.org",
                               content="Le mythe de Prométhée et du feu, sources grecques. " * 45)
        materials = []
        with patch("signal_matin.editorial_v2.tavily_search", return_value=[search_hit]), \
             patch("signal_matin.editorial.article_material",
                   side_effect=lambda news: Material(news.source, news.title,
                                                     news.expanded_summary or news.summary)):
            _research(first, materials, self.config, ResearchBudget(self.config, DATE),
                      RubricDiagnostic(category=first.category), {}, "dossier", "pre-draft",
                      _angles(first), 1, 3)
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].source.url.host, "museum.org")

    def test_monthly_local_budget_persists_between_runs(self):
        self.config["tavily"]["monthly_credit_budget"] = 2
        first = ResearchBudget(self.config, DATE)
        self.assertTrue(first.reserve())
        self.assertTrue(first.reserve())
        second = ResearchBudget(self.config, DATE)
        self.assertFalse(second.reserve())
        self.assertEqual(second.ledger.used(), 2)
        self.assertTrue(ResearchBudget(self.config, dt.date(2026, 11, 1)).reserve())

    def test_logs_never_include_secrets(self):
        first = item()
        with patch("signal_matin.editorial_v2.article_material",
                   return_value=Material(first.source, first.title, first.summary)), \
             patch("signal_matin.editorial_v2.tavily_search",
                   side_effect=TavilyError("secret-tavily-v2")), \
             self.assertLogs("signal_matin.editorial", level="INFO") as logs:
            write_features(self.config, DATE, ["Ingénierie"], [first])
        text = "\n".join(logs.output)
        self.assertNotIn("secret-llm-v2", text)
        self.assertNotIn("secret-tavily-v2", text)

    def test_theoretical_cost_appears_on_last_pdf_page(self):
        source = SourceRef(name="NASA", title=TITLE, url="https://nasa.gov/original")
        paragraphs = [ArticleParagraph(kind=kind, heading=f"Partie {index}" if index < 3 else "",
                                       text=(f"Point historique documenté numéro {index}. " * 30),
                                       source_ids=[1]) for index, kind in enumerate(
            ("facts", "context", "mechanisms", "analysis", "consequences", "limits"))]
        feature = FeatureArticle(category="Histoire", title=TITLE, tier="dossier",
                                 paragraphs=paragraphs, sources=[source])
        edition = MorningEdition(generated_at=WHEN, demo=True, personal_journal=True,
                                 edition=EditionMeta(date=DATE, number=1, title="V2"),
                                 personal_features=[feature], api_cost=ApiCost(
                                     model="gpt-4.1-mini", llm_calls=3, input_tokens=4000,
                                     output_tokens=2000, tavily_searches=3,
                                     llm_usd=0.0048, tavily_usd=0.024, total_usd=0.0288))
        path = Path(self.temp.name) / "v2.pdf"
        reader = PdfReader(str(generer_pdf(edition, path)))
        self.assertIn("COÛT API THÉORIQUE", reader.pages[-1].extract_text())
        self.assertIn("Tavily : 3 recherches", reader.pages[-1].extract_text())


if __name__ == "__main__":
    unittest.main()
