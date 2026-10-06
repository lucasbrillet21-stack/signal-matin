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
from signal_matin.editorial import categories_for_date, fallback_categories, write_features
from signal_matin.editorial import _same_story_semantic, _international_priority, filter_tavily_hits
from signal_matin.editorial_v2 import ResearchBudget, _research, _select_candidates, _timeless_relevant
from signal_matin.models import (ApiCost, ArticleParagraph, EditionMeta, FeatureArticle,
                                 MorningEdition, NewsItem, RubricDiagnostic, SourceRef,
                                 DataSourceStatus, DataState)
from signal_matin.pdf import generer_pdf
from signal_matin.pipeline import build_live
from signal_matin.source_material import Material
from signal_matin.timeless import remember_published, topic_for_date, topics_for_date


DATE = dt.date(2026, 10, 1)
WHEN = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
TITLE = "NASA reporte la mission Artemis III après un incident moteur"


def item():
    return NewsItem(title=TITLE, category="Ingénierie",
                    summary="NASA reporte Artemis III après un incident moteur. " * 5,
                    source=SourceRef(name="NASA", title=TITLE,
                                     url="https://nasa.gov/initial", published_at=WHEN))


def hit(index: int, length: int = 1600):
    text = (f"Artemis III incident moteur. History and mechanism {index} of the schedule, "
            f"testing data and impact. " * 30)
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

    def test_multilingual_tavily_relevance_keeps_event_and_rejects_other_event(self):
        first = NewsItem(title="L'Ukraine intensifie ses frappes contre les raffineries russes",
                         category="International & Géopolitique",
                         summary="Des frappes ukrainiennes visent des raffineries en Russie.",
                         source=SourceRef(name="Euronews", url="https://euronews.com/ukraine",
                                          published_at=WHEN))
        matching = SearchHit(title="Ukraine ramps up strikes on Russian oil refineries",
                             url="https://cnbc.com/refineries", publisher="cnbc.com",
                             content="Ukraine increased strikes on Russian oil refineries. " * 12,
                             published_at=WHEN)
        unrelated = SearchHit(title="Ukraine announces a new education budget",
                              url="https://other.org/education", publisher="other.org",
                              content="Ukraine's education ministry announced school spending. " * 12,
                              published_at=WHEN)
        other_event = SearchHit(title="Ukraine strikes Russian military base",
                                url="https://other.org/base", publisher="other.org",
                                content="Ukraine attacked a Russian military base. " * 12,
                                published_at=WHEN)
        decisions = []
        with patch("signal_matin.editorial.article_material",
                   side_effect=lambda news: Material(news.source, news.title, news.expanded_summary)):
            selected = filter_tavily_hits(first, [], [matching, unrelated, other_event],
                                          on_decision=lambda hit, status, reason:
                                          decisions.append((hit.url, status, reason)),
                                          relevant=_same_story_semantic)
        self.assertEqual([str(m.source.url) for m in selected], [matching.url])
        self.assertEqual([reason for _, status, reason in decisions if status == "REJETÉ"],
                         ["irrelevant", "irrelevant"])

    def test_greenland_graphite_result_needs_graphite_evidence(self):
        first = NewsItem(title="Le Groenland et ses ressources en graphite et minerais critiques",
                         category="International & Géopolitique",
                         summary="Le Groenland examine les ressources minières et le graphite.",
                         source=SourceRef(name="Euronews", url="https://euronews.com/greenland",
                                          published_at=WHEN))
        related = NewsItem(title="Greenland graphite deposits attract critical minerals interest",
                           category=first.category,
                           summary="Greenland graphite resources and mineral development are discussed.",
                           source=SourceRef(name="Source", url="https://example.org/graphite",
                                            published_at=WHEN))
        other_mineral = NewsItem(title="Greenland rare earth mining project",
                                 category=first.category,
                                 summary="A separate Greenland rare earth extraction project.",
                                 source=SourceRef(name="Source", url="https://example.org/rare-earth",
                                                  published_at=WHEN))
        self.assertTrue(_same_story_semantic(first, related))
        self.assertFalse(_same_story_semantic(first, other_mineral))

    def test_international_ranking_excludes_royal_publishing_subject(self):
        cultural = NewsItem(title="Une personnalité royale lance un projet dans l'édition",
                            category="International & Géopolitique",
                            summary="Le nouveau livre concerne le secteur culturel britannique.",
                            source=SourceRef(name="RSS", url="https://example.org/book"))
        diplomatic = NewsItem(title="Des gouvernements négocient un accord de sécurité",
                              category=cultural.category,
                              summary="Une négociation diplomatique internationale se poursuit.",
                              source=SourceRef(name="RSS", url="https://example.org/diplomacy"))
        self.assertIsNone(_international_priority(cultural))
        self.assertGreater(_international_priority(diplomatic), 0)

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
        self.assertEqual(tavily.call_count, 2)
        self.assertGreaterEqual(len(materials), 3)

    def test_poor_documentation_uses_bounded_fallback_queries_and_no_llm(self):
        first = item()
        rows = []
        def material_for(news):
            return Material(news.source, news.title, news.summary)
        with patch("signal_matin.editorial_v2.article_material", side_effect=material_for), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily, \
             patch("signal_matin.editorial_v2.compose_feature") as writer:
            features = write_features(self.config, DATE, ["Ingénierie"], [first], diagnostics=rows)
        self.assertEqual(features, [])
        self.assertEqual(tavily.call_count, 2)
        self.assertEqual(rows[0].tavily_searches, 2)
        writer.assert_not_called()

    def test_pre_draft_queries_are_not_stopped_by_two_empty_results(self):
        first = item()
        row = RubricDiagnostic(category=first.category)
        queries = [(f"{TITLE} angle {index}", f"angle {index}") for index in range(3)]
        with patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily:
            outcome = _research(first, [], self.config, ResearchBudget(self.config, DATE),
                                row, {}, "dossier", "pre-draft", queries, 3, 3)
        self.assertEqual(tavily.call_count, 3)
        self.assertEqual(outcome.searches, 3)

    def test_model_selection_ranks_candidates_and_supplies_targeted_queries(self):
        first = item()
        second = first.model_copy(update={"title": "Une nouvelle méthode de stockage solaire",
                                          "summary": "Une étude décrit une méthode de stockage solaire.",
                                          "source": SourceRef(name="Revue", url="https://example.org/solar",
                                                              published_at=WHEN)})
        entries = [
            {"id": "Ingénierie:0", "recommend": False, "interest": 0.2, "depth": 0.3,
             "documentability": 0.4, "angle": "report", "gap": "chronologie",
             "search_queries": ["Artemis chronologie"]},
            {"id": "Ingénierie:1", "recommend": True, "interest": 0.9, "depth": 0.8,
             "documentability": 0.8, "angle": "fonctionnement du stockage",
             "gap": "étude primaire", "search_queries": ["stockage solaire étude primaire"]},
        ]
        with patch("signal_matin.editorial_v2._chat",
                   return_value=json.dumps({"candidates": entries})) as chat:
            ranked, queries = _select_candidates({"Ingénierie": [first, second]})
        self.assertIs(ranked["Ingénierie"][0], second)
        self.assertEqual(len(ranked["Ingénierie"]), 1)
        self.assertEqual(queries[id(second)], [("stockage solaire étude primaire", "étude primaire")])
        schema = chat.call_args.kwargs["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])

    def test_model_selection_is_one_call_before_tavily_and_invalid_result_falls_back(self):
        first = item()
        self.config["tavily"]["max_searches_per_edition"] = 1
        with patch.dict(os.environ, {"SIGNAL_MATIN_LLM_URL":
                                       "https://api.openai.com/v1/chat/completions"}), \
             patch("signal_matin.editorial_v2._chat", return_value='{"candidates": []}') as selector, \
             patch("signal_matin.editorial_v2.article_material",
                   return_value=Material(first.source, first.title, first.summary)), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily:
            features = write_features(self.config, DATE, ["Ingénierie"], [first])
        self.assertEqual(features, [])
        self.assertEqual(selector.call_count, 1)
        self.assertEqual(tavily.call_count, 1)

    def test_model_rejection_prevents_search_on_uninteresting_candidate(self):
        first = item()
        second = first.model_copy(update={"title": "Une nouvelle méthode de stockage solaire",
                                          "summary": "Une étude décrit une méthode de stockage solaire.",
                                          "source": SourceRef(name="Revue", url="https://example.org/solar",
                                                              published_at=WHEN)})
        choices = [
            {"id": "Ingénierie:0", "recommend": False, "interest": 0.1, "depth": 0.2,
             "documentability": 0.2, "angle": "", "gap": "sujet limité", "search_queries": []},
            {"id": "Ingénierie:1", "recommend": True, "interest": 0.9, "depth": 0.8,
             "documentability": 0.8, "angle": "méthode et limites", "gap": "étude primaire",
             "search_queries": ["stockage solaire étude primaire"]},
        ]
        self.config["tavily"]["max_searches_per_edition"] = 1
        with patch.dict(os.environ, {"SIGNAL_MATIN_LLM_URL":
                                       "https://api.openai.com/v1/chat/completions"}), \
             patch("signal_matin.editorial_v2._chat",
                   return_value=json.dumps({"candidates": choices})), \
             patch("signal_matin.editorial_v2.article_material",
                   side_effect=lambda news: Material(news.source, news.title, news.summary)), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily:
            write_features(self.config, DATE, ["Ingénierie"], [first, second])
        self.assertEqual(tavily.call_count, 1)
        self.assertEqual(tavily.call_args.args[0], "stockage solaire étude primaire")

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

    def test_post_critique_does_not_repeat_a_failed_gap(self):
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
        self.assertEqual(tavily.call_count, 1)
        self.assertEqual(tavily.call_args.args[0], queries[0])

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
        fallback = fallback_categories(self.config, ["Sciences & Curiosités", "Ingénierie",
                                                     "Intelligence artificielle"])
        self.assertIn("Histoire", fallback)
        self.assertLess(fallback.index("Histoire"), fallback.index("Culture"))
        self.assertEqual(categories_for_date(self.config, dt.date(2026, 10, 4))[-1],
                         "Mythologies & Religions")
        history = topic_for_date(self.config, "Histoire", DATE)
        myth = topic_for_date(self.config, "Mythologies & Religions", DATE)
        self.assertIsNotNone(history)
        self.assertIsNotNone(myth)
        philosophy = topic_for_date(self.config, "Philosophie", DATE)
        self.assertIsNotNone(philosophy)
        self.assertIsNone(philosophy.source.url)
        self.assertIsNone(history.source.published_at)
        self.assertIsNone(history.source.url)
        self.assertIn("source factuelle", history.summary)
        remember_published("Histoire", history.title, DATE)
        later = topic_for_date(self.config, "Histoire", DATE)
        self.assertNotEqual(later.title, history.title)
        self.assertGreater(len(topics_for_date(self.config, "Histoire", DATE)), 1)

    def test_philosophy_research_starts_from_topic_without_recent_rss(self):
        self.config["tavily"]["max_pre_draft_searches"] = 1
        topic = topic_for_date(self.config, "Philosophie", DATE)
        rows = []
        with patch("signal_matin.editorial_v2.tavily_search", return_value=[]) as tavily, \
             patch("signal_matin.editorial_v2.article_material") as extraction, \
             patch("signal_matin.editorial_v2.compose_feature") as writer:
            features = write_features(self.config, DATE, ["Philosophie"], [], diagnostics=rows)
        self.assertEqual(features, [])
        self.assertEqual(rows[0].candidates_tried, 1)
        self.assertIn(topic.title, tavily.call_args.args[0])
        extraction.assert_not_called()
        writer.assert_not_called()

    def test_philosophy_matches_documented_bilingual_subject(self):
        first = NewsItem(title="Le libre arbitre", category="Philosophie",
                         summary="Sujet documentaire intemporel.",
                         source=SourceRef(name="Banque de sujets"))
        related = NewsItem(title="Free Will - Stanford Encyclopedia of Philosophy",
                           category=first.category, summary="Arguments about free will.",
                           source=SourceRef(name="Stanford", url="https://plato.stanford.edu/entries/freewill/"))
        unrelated = NewsItem(title="The Problem of Evil", category=first.category,
                             summary="Philosophical discussion of evil.",
                             source=SourceRef(name="Stanford", url="https://example.org/evil"))
        self.assertTrue(_timeless_relevant(first, related))
        self.assertFalse(_timeless_relevant(first, unrelated))

    def test_timeless_source_filter_rejects_social_and_thin_snippets(self):
        first = NewsItem(title="La Bhagavad-Gita", category="Mythologies & Religions",
                         summary="Sujet documentaire intemporel.",
                         source=SourceRef(name="Banque de sujets"))
        hits = [SearchHit(title="La Bhagavad-Gita", url="https://amazon.com/book",
                          publisher="amazon.com", content="Bhagavad Gita " * 40),
                SearchHit(title="Bhagavad Gita - aperçu", url="https://other.org/short",
                          publisher="other.org", content="Bhagavad Gita " * 10),
                SearchHit(title="Bhagavad Gita - Historical Study", url="https://university.edu/gita",
                          publisher="university.edu", content="Bhagavad Gita " * 40)]
        decisions = []
        with patch("signal_matin.editorial.article_material",
                   side_effect=lambda news: Material(news.source, news.title, news.expanded_summary)):
            kept = filter_tavily_hits(first, [], hits, relevant=_timeless_relevant,
                                      on_decision=lambda hit, status, reason:
                                      decisions.append((hit.publisher, status, reason)))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source.url.host, "university.edu")
        self.assertIn(("amazon.com", "REJETÉ", "non_documentary_domain"), decisions)
        self.assertIn(("other.org", "REJETÉ", "content_too_short"), decisions)

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

    def test_edition_budget_reserves_each_rubric_and_releases_unused(self):
        categories = ["Ingénierie", "Économie", "Histoire"]
        budget = ResearchBudget(self.config, DATE, categories)
        self.assertEqual(budget.limit, 30)
        self.assertEqual(sum(budget.reserve(categories[0]) for _ in range(30)), 10)
        self.assertEqual(budget.last_denial, "edition_or_category_budget")
        self.assertTrue(budget.reserve(categories[1]))
        budget.release(categories[1])
        self.assertEqual(sum(budget.reserve(categories[2]) for _ in range(30)), 19)
        self.assertEqual(budget.used, 30)

    def test_productive_searches_can_reach_ten_call_ceiling(self):
        first = item()
        materials = []
        budget = ResearchBudget(self.config, DATE)
        row = RubricDiagnostic(category=first.category)
        queries = [(f"{TITLE} research {i}", f"angle {i}") for i in range(10)]
        results = [[hit(i, 500)] for i in range(10)]
        with patch("signal_matin.editorial_v2.tavily_search", side_effect=results) as tavily, \
             patch("signal_matin.editorial.article_material",
                   side_effect=lambda news: Material(news.source, news.title,
                                                     news.expanded_summary or news.summary)), \
             patch("signal_matin.editorial_v2._research_complete", return_value=False):
            outcome = _research(first, materials, self.config, budget, row, {}, "dossier",
                                "pre-draft", queries, 10, 3)
        self.assertEqual(tavily.call_count, 10)
        self.assertEqual(outcome.searches, 10)
        self.assertGreater(outcome.gain_chars, 1000)

    def test_full_rich_dossier_does_not_spend_third_search(self):
        first = item()
        materials = [Material(SourceRef(name=f"Archive {index}",
                                        url=f"https://archive{index}.org/study"), TITLE,
                              "Historical context and mechanism with evidence. " * 20)
                     for index in range(10)]
        budget = ResearchBudget(self.config, DATE)
        with patch("signal_matin.editorial_v2.tavily_search") as tavily:
            outcome = _research(first, materials, self.config, budget,
                                RubricDiagnostic(category=first.category), {}, "dossier",
                                "pre-draft", [(TITLE, "principal")], 10, 3)
        tavily.assert_not_called()
        self.assertEqual(outcome.stop_reason, "documentary_goal_reached")

    def test_narrative_three_part_response_with_s1_references(self):
        from signal_matin.synthesis import ComposeTrace, capture_compose, compose_feature
        first = item()
        materials = [Material(first.source, TITLE, "History, facts and analysis. " * 130)]
        response = {"title": TITLE, "paragraphs": [
            {"heading": heading, "text": (f"{heading} documented account. " * 40),
             "dimensions": dimensions, "source_ids": ["S1"]}
            for heading, dimensions in [
                ("Contexte et faits", ["context", "facts"]),
                ("Mécanismes et enjeux", ["mechanisms", "analysis"]),
                ("Conséquences et limites", ["consequences", "limits"]),
            ]]}
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace):
            article = compose_feature(first.category, "dossier", materials, (180, 900), v2=True)
        self.assertIsNotNone(article)
        self.assertEqual(len(article.sources), 1)
        self.assertEqual(article.paragraphs[0].source_ids, [1])
        self.assertEqual(trace.validation, "OK")

    def test_six_distinct_dimension_headings_become_three_narrative_parts(self):
        from signal_matin.synthesis import ComposeTrace, capture_compose, compose_feature
        first = item()
        response = json.loads(article_json("draft"))
        for part, heading in zip(response["paragraphs"],
                                 ("Faits", "Contexte", "Mécanismes", "Analyse",
                                  "Conséquences", "Limites")):
            part["heading"] = heading
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace):
            article = compose_feature(first.category, "dossier",
                                      [Material(first.source, TITLE, "Documentation. " * 120)],
                                      (450, 900), v2=True)
        self.assertIsNotNone(article)
        self.assertEqual(sum(bool(part.heading) for part in article.paragraphs), 3)
        self.assertEqual(trace.validation, "OK")

    def test_overlong_draft_is_critiqued_but_overlong_rewrite_is_rejected(self):
        from signal_matin.synthesis import ComposeTrace, EditorialCritique, capture_compose, compose_feature
        first = item()
        material = Material(first.source, TITLE, "Verified technical documentation. " * 160)
        response_data = json.loads(article_json("draft"))
        for index, part in enumerate(response_data["paragraphs"]):
            part["text"] += f" Additional documented point {index}. " * 5
        response = json.dumps(response_data)
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=response), capture_compose(trace):
            draft = compose_feature(first.category, "article", [material], (450, 600), v2=True)
        self.assertIsNotNone(draft)
        self.assertGreater(draft.word_count(), 600)
        self.assertEqual(trace.reason, "draft_above_word_limit")
        critique = EditorialCritique.model_validate_json(critique_json())
        trace = ComposeTrace(phase="rewrite")
        with patch("signal_matin.synthesis._chat", return_value=response), capture_compose(trace):
            final = compose_feature(first.category, "article", [material], (450, 600),
                                    revision=(draft, critique), v2=True)
        self.assertIsNone(final)
        self.assertEqual(trace.reason, "above_word_limit")

    def test_overlong_draft_can_be_shortened_in_full_critique_rewrite_loop(self):
        first = item()
        rich = Material(first.source, TITLE, "Historical context and mechanism. " * 180)
        second = first.model_copy(update={"source": SourceRef(name="Archive",
                                                          url="https://archive.org/artemis")})
        other = Material(second.source, TITLE, "Independent context and impact. " * 130)
        draft = json.loads(article_json("draft"))
        for index, part in enumerate(draft["paragraphs"]):
            part["text"] = (f"Verified historical context {index}, mechanism and consequence. " * 26)
        final = json.loads(article_json("final"))
        for index, part in enumerate(final["paragraphs"]):
            part["text"] = (f"Verified context {index}, mechanism and consequence. " * 20)
        rows = []
        with patch("signal_matin.editorial_v2.article_material",
                   side_effect=lambda news: rich if news.source.name == "NASA" else other), \
             patch("signal_matin.synthesis._chat",
                   side_effect=[json.dumps(draft), critique_json(), json.dumps(final)]) as chat, \
             patch("signal_matin.editorial_v2.tavily_search"):
            features = write_features(self.config, DATE, [first.category], [first, second], diagnostics=rows)
        self.assertEqual(len(features), 1)
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(rows[0].result, "published")
        self.assertLessEqual(features[0].word_count(), 900)

    def test_overlong_rewrite_gets_one_bounded_compression_pass(self):
        first = item()
        second = first.model_copy(update={"source": SourceRef(name="Archive",
                                                          url="https://archive.org/artemis")})
        materials = {"NASA": Material(first.source, TITLE, "Historical context. " * 180),
                     "Archive": Material(second.source, TITLE, "Independent mechanism. " * 180)}
        overlong = json.loads(article_json("long"))
        compressed = json.loads(article_json("compressed"))
        for index, part in enumerate(overlong["paragraphs"]):
            part["text"] = f"Documented detail {index} with context and mechanism. " * 26
        for index, part in enumerate(compressed["paragraphs"]):
            part["text"] = f"Documented detail {index} with context and mechanism. " * 18
        with patch("signal_matin.editorial_v2.article_material",
                   side_effect=lambda news: materials[news.source.name]), \
             patch("signal_matin.synthesis._chat",
                   side_effect=[article_json("draft"), critique_json(),
                                json.dumps(overlong), json.dumps(compressed)]) as chat, \
             patch("signal_matin.editorial_v2.tavily_search"):
            features = write_features(self.config, DATE, [first.category], [first, second])
        self.assertEqual(len(features), 1)
        self.assertEqual(chat.call_count, 4)
        self.assertLessEqual(features[0].word_count(), 900)
        self.assertIn("COMPRESSION FINALE OBLIGATOIRE", chat.call_args.args[0][-1]["content"])

    def test_v2_does_not_draft_from_single_short_source(self):
        first = item()
        thin = Material(first.source, TITLE, "Short public brief. " * 74)
        rows = []
        with patch("signal_matin.editorial_v2.article_material", return_value=thin), \
             patch("signal_matin.editorial_v2.tavily_search", return_value=[]), \
             patch("signal_matin.editorial_v2.compose_feature") as writer:
            features = write_features(self.config, DATE, [first.category], [first], diagnostics=rows)
        self.assertEqual(features, [])
        self.assertEqual(rows[0].result, "tavily_insufficient")
        self.assertEqual(rows[0].reason, "no_results_retained")
        writer.assert_not_called()

    def test_unknown_dimension_is_ignored_only_when_required_six_remain(self):
        from signal_matin.synthesis import ComposeTrace, capture_compose, compose_feature
        first = item()
        material = Material(first.source, TITLE, "Verified technical documentation. " * 120)
        response = json.loads(article_json("draft"))
        response["paragraphs"][0]["dimensions"] = ["facts", "autre"]
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace):
            self.assertIsNotNone(compose_feature(first.category, "dossier", [material],
                                                  (450, 900), v2=True))
        response["paragraphs"][0]["dimensions"] = ["autre"]
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace):
            self.assertIsNone(compose_feature(first.category, "dossier", [material],
                                               (450, 900), v2=True))
        self.assertEqual(trace.reason, "missing_editorial_dimension")

    def test_critic_malformed_response_reports_exact_field(self):
        from signal_matin.synthesis import critique_feature
        first = item()
        material = Material(first.source, TITLE, "Documented mechanism. " * 120)
        response = json.loads(critique_json())
        response.pop("missing_context")
        article = FeatureArticle(category=first.category, title=TITLE, tier="dossier",
                                 paragraphs=[ArticleParagraph(kind="facts", text="Documented fact. " * 20,
                                                              source_ids=[1])], sources=[first.source])
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             self.assertLogs("signal_matin.synthesis") as logs:
            with self.assertRaises(ValueError):
                critique_feature(article, [material])
        self.assertIn("phase=critique reason=invalid_critique field=missing_context type=missing",
                      "\n".join(logs.output))

    def test_openai_critic_request_requires_every_json_field(self):
        from signal_matin.synthesis import critique_feature
        first = item()
        article = FeatureArticle(category=first.category, title=TITLE, tier="dossier",
                                 paragraphs=[ArticleParagraph(kind="facts", text="Documented. " * 20,
                                                              source_ids=[1])], sources=[first.source])
        material = Material(first.source, TITLE, "Documented source. " * 100)
        response = {"choices": [{"message": {"content": critique_json()}}]}
        sent = []
        def fake_open(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps(response).encode())
        with patch.dict(os.environ, {"SIGNAL_MATIN_LLM_URL":
                                       "https://api.openai.com/v1/chat/completions"}), \
             patch("signal_matin.synthesis.urllib.request.urlopen", side_effect=fake_open):
            critique_feature(article, [material])
        schema = sent[0]["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(set(schema["schema"]["required"]),
                         set(schema["schema"]["properties"]))
        self.assertFalse(schema["schema"]["additionalProperties"])

    def test_invalid_s7_and_missing_dimension_have_precise_safe_logs(self):
        from signal_matin.synthesis import ComposeTrace, capture_compose, compose_feature
        first = item()
        materials = [Material(first.source, TITLE, "History and facts. " * 100)]
        response = json.loads(article_json("draft"))
        response["paragraphs"][0]["source_ids"] = ["S7"]
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace), self.assertLogs("signal_matin.synthesis") as logs:
            self.assertIsNone(compose_feature(first.category, "dossier", materials,
                                               (180, 900), v2=True))
        self.assertEqual(trace.reason, "invalid_source_reference")
        self.assertIn("reference=S7 available=S1..S1", "\n".join(logs.output))
        self.assertNotIn("secret-llm-v2", "\n".join(logs.output))
        response["paragraphs"][0]["source_ids"] = ["S1"]
        response["paragraphs"].pop()
        trace = ComposeTrace(phase="rewrite")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace), self.assertLogs("signal_matin.synthesis") as logs:
            self.assertIsNone(compose_feature(first.category, "dossier", materials,
                                               (180, 900), v2=True))
        self.assertEqual(trace.reason, "missing_editorial_dimension")
        self.assertIn("missing=limits", "\n".join(logs.output))

    def test_empty_section_and_duplicate_heading_are_named(self):
        from signal_matin.synthesis import ComposeTrace, capture_compose, compose_feature
        first = item()
        materials = [Material(first.source, TITLE, "Document technique vérifié. " * 100)]
        response = json.loads(article_json("draft"))
        response["paragraphs"][0]["text"] = "   "
        trace = ComposeTrace(phase="draft")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace):
            self.assertIsNone(compose_feature(first.category, "dossier", materials,
                                               (180, 900), v2=True))
        self.assertEqual(trace.reason, "empty_section")
        response = json.loads(article_json("draft"))
        response["paragraphs"][2]["heading"] = response["paragraphs"][0]["heading"]
        trace = ComposeTrace(phase="rewrite")
        with patch("signal_matin.synthesis._chat", return_value=json.dumps(response)), \
             capture_compose(trace), self.assertLogs("signal_matin.synthesis") as logs:
            self.assertIsNone(compose_feature(first.category, "dossier", materials,
                                               (180, 900), v2=True))
        self.assertEqual(trace.reason, "duplicated_heading")
        self.assertIn("phase=rewrite heading=contexte et faits", "\n".join(logs.output).casefold())

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

    def test_email_exit_one_only_when_no_accepted_article(self):
        from signal_matin.cli import main
        row = RubricDiagnostic(category="Ingénierie", result="article_rejected",
                               reason="missing_editorial_dimension")
        edition = MorningEdition(generated_at=WHEN, demo=False, personal_journal=True,
                                 edition=EditionMeta(date=DATE, number=1, title="V2"),
                                 personal_diagnostics=[row])
        with patch("signal_matin.cli._edition", return_value=edition), \
             patch("signal_matin.cli.ecrire_edition"), patch("signal_matin.cli.write_html"), \
             patch("signal_matin.cli.generer_pdf"), patch("signal_matin.cli.send_pdf") as email:
            with self.assertRaises(SystemExit) as raised:
                main(["generate", "--config", "config.personal.example.yaml", "--live",
                      "--date", DATE.isoformat(), "--output", self.temp.name, "--email"])
        self.assertIn("article_rejected (missing_editorial_dimension)", str(raised.exception))
        email.assert_not_called()

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
