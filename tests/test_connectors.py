import datetime as dt
import pytest

from signal_matin.connectors.rss import collect_rss
from signal_matin.models import DataState, NewsItem, SourceRef
from signal_matin.source_material import article_material


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Une information de test</title><link>https://example.org/a</link>
<description>Un resume entierement fictif pour le test du connecteur.</description>
<pubDate>Sat, 26 Sep 2026 06:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_rss_is_normalized(monkeypatch):
    monkeypatch.setattr("signal_matin.connectors.rss._payload", lambda _url: RSS)
    now = dt.datetime(2026, 9, 26, 8, tzinfo=dt.timezone.utc)
    items, status = collect_rss([
        {"name": "Source test", "category": "Monde", "url": "https://example.org/rss"}
    ], now)
    assert status.state == DataState.LIVE
    assert items[0].title == "Une information de test"
    assert items[0].source.name == "Source test"


def test_empty_rss_is_optional():
    items, status = collect_rss([], dt.datetime.now().astimezone())
    assert items == []
    assert status.state == DataState.DISABLED


def test_rss_keeps_rich_encoded_content_and_provenance(monkeypatch):
    body = "Texte documenté complet. " * 60
    payload = ("<?xml version='1.0'?><rss xmlns:content='http://purl.org/rss/1.0/modules/content/'><channel>"
               "<item><title>Étude publique</title><link>https://example.org/etude</link>"
               "<description>Résumé court.</description>"
               f"<content:encoded><![CDATA[<p>{body}</p>]]></content:encoded>"
               "<pubDate>Sat, 26 Sep 2026 06:00:00 +0000</pubDate>"
               "</item></channel></rss>").encode()
    monkeypatch.setattr("signal_matin.connectors.rss._payload", lambda _url: payload)
    items, _ = collect_rss([{"name": "Source test", "category": "Sciences",
                             "url": "https://example.org/rss"}],
                           dt.datetime(2026, 9, 26, 8, tzinfo=dt.timezone.utc))
    assert items[0].summary == "Résumé court."
    assert items[0].expanded_summary == body.strip()
    assert str(items[0].source.url) == "https://example.org/etude"
    assert items[0].source.published_at is not None


@pytest.mark.parametrize("field", ["description", "summary", "content"])
def test_rss_long_fields_are_not_cut_at_300(monkeypatch, field):
    body = "Long contenu vérifiable. " * 50
    payload = (f"<feed><entry><title>Analyse</title><link href='https://example.org/a'/>"
               f"<{field}>{body}</{field}>"
               "<updated>2026-09-26T06:00:00Z</updated></entry></feed>").encode()
    monkeypatch.setattr("signal_matin.connectors.rss._payload", lambda _url: payload)
    items, _ = collect_rss([{"url": "https://example.org/rss"}],
                           dt.datetime(2026, 9, 26, 8, tzinfo=dt.timezone.utc))
    assert len(items[0].expanded_summary) > 900
    assert items[0].expanded_summary == body.strip()


def test_article_material_uses_rich_rss_when_page_is_disallowed(monkeypatch):
    monkeypatch.setattr("signal_matin.source_material._robots_allow", lambda _url: False)
    item = NewsItem(title="Analyse", summary="Résumé court", expanded_summary="Matière publique. " * 70,
                    source=SourceRef(name="Source", url="https://example.org/a"))
    material = article_material(item)
    assert len(material.text) > 900
    assert str(material.source.url) == "https://example.org/a"


def test_generic_feed_only_keeps_items_matching_category_keywords(monkeypatch):
    payload = b"""<rss><channel>
      <item><title>Earth geology survey</title><link>https://example.org/earth</link>
        <description>Researchers study rocks.</description>
        <pubDate>Sat, 26 Sep 2026 06:00:00 +0000</pubDate></item>
      <item><title>New art exhibition</title><link>https://example.org/art</link>
        <description>A museum opens an exhibition.</description>
        <pubDate>Sat, 26 Sep 2026 06:00:00 +0000</pubDate></item>
    </channel></rss>"""
    monkeypatch.setattr("signal_matin.connectors.rss._payload", lambda _url: payload)
    items, _ = collect_rss([{"name": "Generic", "category": "Culture",
                             "url": "https://example.org/feed", "include_any": ["art", "museum"]}],
                           dt.datetime(2026, 9, 26, 8, tzinfo=dt.timezone.utc))
    assert [item.title for item in items] == ["New art exhibition"]
