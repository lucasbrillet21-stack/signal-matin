"""Workflow et diagnostic Tavily : tests entièrement hors réseau."""
import contextlib
import datetime as dt
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import tavily_diagnostic
from signal_matin.config import load_config
from signal_matin.connectors.tavily import SearchHit, TavilyError
from signal_matin.models import NewsItem, SourceRef
from signal_matin.source_material import Material


DATE = dt.datetime(2026, 10, 1, 5, tzinfo=dt.timezone.utc)
TITLE = "NASA reporte la mission Artemis III après un incident moteur"
SUMMARY = ("La mission Artemis III est reportée après un incident moteur. "
           "La NASA doit réexaminer le calendrier des essais. " * 3)[:300]


def subject():
    return NewsItem(title=TITLE, category="Ingénierie", summary=SUMMARY,
                    source=SourceRef(name="NASA", title=TITLE,
                                     url="https://nasa.gov/initial", published_at=DATE))


def hit(domain, path, content):
    return SearchHit(title=f"NASA reporte Artemis III après incident moteur : {domain}",
                     url=f"https://{domain}/{path}", content=content,
                     publisher=domain, published_at=DATE, score=0.9)


def material(item):
    return Material(item.source, item.title, item.expanded_summary or item.summary)


class TavilyDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.personal.example.yaml")
        self.config["editorial"]["v2"]["enabled"] = False
        self.config["tavily"]["max_searches_per_edition"] = 6

    def test_workflow_is_manual_and_receives_only_tavily_secret(self):
        path = Path(".github/workflows/tavily-diagnostic.yml")
        raw = path.read_text(encoding="utf-8")
        workflow = yaml.load(raw, Loader=yaml.BaseLoader)
        self.assertEqual(workflow["name"], "Tavily diagnostic")
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(raw.count("secrets."), 1)
        self.assertIn("secrets.TAVILY_API_KEY", raw)
        self.assertNotIn("main.py", raw)
        self.assertNotIn("--email", raw)
        self.assertNotIn("journal.yml", raw)
        self.assertEqual(workflow["jobs"]["diagnostic"]["steps"][-1]["run"],
                         "python scripts/tavily_diagnostic.py")

    def test_one_search_and_success_when_documentation_stays_short(self):
        output = io.StringIO()
        key = "private-test-key"
        with patch.dict(os.environ, {"TAVILY_API_KEY": key}), \
             patch("scripts.tavily_diagnostic.select_subject", return_value=subject()), \
             patch("scripts.tavily_diagnostic.article_material", side_effect=material), \
             patch("signal_matin.editorial.article_material", side_effect=material), \
             patch("scripts.tavily_diagnostic.tavily_search", return_value=[
                 hit("agency.org", "a", "Artemis III incident moteur. " * 12)]) as search, \
             contextlib.redirect_stdout(output):
            tavily_diagnostic.run_diagnostic(self.config, DATE)
        report = output.getvalue()
        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs, {"max_results": 8, "recent": True})
        self.assertIn("Recherche Tavily : 1/1", report)
        self.assertIn("Résultats reçus : 1", report)
        self.assertIn("Résultats retenus : 1", report)
        self.assertIn("Dossier suffisamment documenté : NON", report)
        self.assertNotIn(key, report)

    def test_filtered_results_keep_only_relevant_distinct_urls(self):
        text = "Artemis III incident moteur. " + "NASA détaille les essais. " * 70
        output = io.StringIO()
        with patch.dict(os.environ, {"TAVILY_API_KEY": "fake"}), \
             patch("scripts.tavily_diagnostic.select_subject", return_value=subject()), \
             patch("scripts.tavily_diagnostic.article_material", side_effect=material), \
             patch("signal_matin.editorial.article_material", side_effect=material), \
             patch("scripts.tavily_diagnostic.tavily_search", return_value=[
                 hit("agency.org", "a", text),
                 hit("agency.org", "a?utm_source=x", text),
                 SearchHit("Sujet sans rapport", "https://irrelevant.org/b", "Texte différent. " * 60,
                           "irrelevant.org", DATE),
             ]) as search, contextlib.redirect_stdout(output):
            tavily_diagnostic.run_diagnostic(self.config, DATE)
        report = output.getvalue()
        search.assert_called_once()
        self.assertIn("Résultats reçus : 3", report)
        self.assertIn("Résultats retenus : 1", report)
        self.assertIn("Domaine : agency.org", report)
        self.assertIn("URL : https://agency.org/a", report)
        self.assertNotIn("irrelevant.org", report)
        self.assertIn("Seuil documentaire >= 900 : OUI", report)

    def test_api_error_fails_without_disclosing_secret(self):
        err = io.StringIO()
        key = "private-test-key"
        with patch.dict(os.environ, {"TAVILY_API_KEY": key}), \
             patch("scripts.tavily_diagnostic.select_subject", return_value=subject()), \
             patch("scripts.tavily_diagnostic.article_material", side_effect=material), \
             patch("scripts.tavily_diagnostic.tavily_search", side_effect=TavilyError("HTTP 401")) as search, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            status = tavily_diagnostic.main()
        self.assertEqual(status, 1)
        search.assert_called_once()
        self.assertIn("HTTP 401", err.getvalue())
        self.assertNotIn(key, err.getvalue())

    def test_absent_key_fails_before_any_rss_or_search(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}), \
             patch("scripts.tavily_diagnostic.select_subject") as select, \
             patch("scripts.tavily_diagnostic.tavily_search") as search:
            with self.assertRaises(TavilyError):
                tavily_diagnostic.run_diagnostic(self.config, DATE)
        select.assert_not_called()
        search.assert_not_called()

    def test_subject_selection_requires_real_dated_rss_material(self):
        short = subject().model_copy(update={"summary": "Bref.", "expanded_summary": ""})
        with patch("scripts.tavily_diagnostic.collect_rss", return_value=([short, subject()], None)) as rss:
            chosen = tavily_diagnostic.select_subject(self.config, DATE)
        self.assertEqual(chosen.title, TITLE)
        self.assertGreaterEqual(len(chosen.summary), 120)
        self.assertTrue(rss.call_args.kwargs["require_date"])
        self.assertEqual(rss.call_args.kwargs["limit"], 8)

    def test_no_recent_rss_subject_is_inconclusive_not_api_failure(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"TAVILY_API_KEY": "fake"}), \
             patch("scripts.tavily_diagnostic.collect_rss", return_value=([], None)), \
             patch("scripts.tavily_diagnostic.tavily_search") as search, \
             contextlib.redirect_stdout(output):
            status = tavily_diagnostic.main()
        self.assertEqual(status, 0)
        self.assertIn("Non exécuté", output.getvalue())
        self.assertIn("Recherche Tavily : 0/1", output.getvalue())
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
