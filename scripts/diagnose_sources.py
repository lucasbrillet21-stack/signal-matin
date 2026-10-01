"""Read-only HTTP diagnostic for candidate editorial sources; no LLM or email calls."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from signal_matin.connectors.rss import _date, _link, _payload, _tag, feed_texts  # noqa: E402
from signal_matin.source_material import USER_AGENT, _ArticleParser  # noqa: E402


@dataclass(frozen=True)
class Candidate:
    name: str
    categories: str
    url: str
    kind: str = "feed"
    sample_url: str = ""


# Each URL is a public feed/API endpoint, never a login, paywall or cache proxy.
CANDIDATES = [
    Candidate("Le Monde / International", "International", "https://www.lemonde.fr/international/rss_full.xml"),
    Candidate("Le Monde / Economie", "Économie", "https://www.lemonde.fr/economie/rss_full.xml"),
    Candidate("Le Monde / IA", "Intelligence artificielle", "https://www.lemonde.fr/intelligence-artificielle/rss_full.xml"),
    Candidate("Euronews", "International, Géopolitique", "https://fr.euronews.com/rss?format=mrss&level=theme&name=news"),
    Candidate("Conseil de l'UE", "Géopolitique, International", "https://www.consilium.europa.eu/en/rss/pressreleases.ashx"),
    Candidate("Documents ONU", "International, Géopolitique", "https://www.un.org/en/our-work/documents", "page"),
    Candidate("Eurostat / economie", "Économie", "https://ec.europa.eu/eurostat/en/search?_estatsearchportlet_WAR_estatsearchportlet_collection=CAT_EURNEW&_estatsearchportlet_WAR_estatsearchportlet_theme=PER_ECOFIN&p_p_id=estatsearchportlet_WAR_estatsearchportlet&p_p_lifecycle=2&p_p_mode=view&p_p_resource_id=atom&p_p_state=maximized"),
    Candidate("BCE / communiques", "Économie", "https://www.ecb.europa.eu/rss/press.html"),
    Candidate("NASA", "Sciences, Ingénierie, Curiosités", "https://www.nasa.gov/feed/"),
    Candidate("NASA / technologie", "Ingénierie, Sciences", "https://www.nasa.gov/technology/feed/"),
    Candidate("Commission UE / industrie", "Ingénierie, Économie", "https://single-market-economy.ec.europa.eu/node/2046/rss_en"),
    Candidate("JPL", "Sciences, Ingénierie", "https://www.jpl.nasa.gov/feeds/news/"),
    Candidate("arXiv / IA", "Intelligence artificielle, Informatique", "https://rss.arxiv.org/rss/cs.AI"),
    Candidate("Cloudflare / ingenierie", "Informatique, Ingénierie", "https://blog.cloudflare.com/tag/engineering/rss"),
    Candidate("Cloudflare / IA", "Intelligence artificielle", "https://blog.cloudflare.com/tag/artificial-intelligence/rss"),
    Candidate("GitHub Blog", "Informatique", "https://github.blog/feed/"),
    Candidate("SEP", "Philosophie", "https://plato.stanford.edu/rss/sep.xml"),
    Candidate("BnF / actualites", "Littérature, Histoire, Culture", "https://www.bnf.fr/fr/actualites/rss"),
    Candidate("Library of Congress", "Histoire, Littérature, Culture, Curiosités", "https://www.loc.gov/rss/pao/news.xml"),
    Candidate("Smithsonian", "Sciences, Histoire, Culture, Curiosités", "https://www.smithsonianmag.com/rss/latest_articles/"),
    Candidate("UNESCO patrimoine", "Culture, Histoire", "https://whc.unesco.org/en/news/rss/"),
    Candidate("Gallica OPDS", "Philosophie, Littérature, Histoire", "https://gallica.bnf.fr/opds"),
    Candidate("ReliefWeb API", "International, Géopolitique", "https://api.reliefweb.int/v2/reports?appname=signal-matin&limit=5", "api"),
    Candidate("MusicBrainz API", "Musique", "https://musicbrainz.org/ws/2/release-group/?query=tag:post-punk&fmt=json&limit=5", "api"),
    Candidate("Bandcamp Daily / rock", "Musique", "https://daily.bandcamp.com/genres/rock", "page",
              "https://daily.bandcamp.com/label-profile/heavy-blessings-a-church-road-records-primer"),
]


def fetch(url: str) -> tuple[int, str, str, bytes, str]:
    """Fetch with verified TLS via curl's OS trust store and report the final URL."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("HTTPS requis")
    command = ["curl", "--location", "--silent", "--show-error", "--max-time", "12",
               "--max-filesize", "2000000", "--proto", "=https", "--proto-redir", "=https",
               "--user-agent", USER_AGENT, "--write-out",
               "\n__SIGNAL_META__%{http_code}|%{url_effective}|%{content_type}", url]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    body, marker, meta = result.stdout.rpartition(b"\n__SIGNAL_META__")
    if not marker:
        return 0, url, "", b"", result.stderr.decode("utf-8", "replace").strip()[:160]
    code, final_url, content_type = meta.decode("utf-8", "replace").split("|", 2)
    error = result.stderr.decode("utf-8", "replace").strip()[:160] if result.returncode else ""
    return int(code), final_url, content_type, body, error


def article_probe(url: str) -> tuple[str, int, str, str]:
    """Inspect one public page only when its robots.txt allows this user agent."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return "non", 0, url, "URL non HTTPS"
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    robots_code, _, _, robots, robots_error = fetch(robots_url)
    if robots_code not in {200, 404, 410} or robots_error:
        return "indéterminé", 0, url, f"robots.txt: HTTP {robots_code} {robots_error}".strip()
    parser = RobotFileParser()
    parser.parse(robots.decode("utf-8", "replace").splitlines() if robots_code == 200 else [])
    if not parser.can_fetch(USER_AGENT, url):
        return "non", 0, url, "robots.txt interdit la lecture"
    code, final_url, content_type, body, error = fetch(url)
    if code != 200 or error:
        return "non", 0, final_url, f"HTTP {code} {error}".strip()
    if urllib.parse.urlsplit(final_url).netloc != parts.netloc:
        return "indéterminé", 0, final_url, "redirection vers un autre domaine non vérifié"
    if "html" not in content_type:
        return "non", 0, final_url, f"format {content_type} non extrait"
    page = body.decode("utf-8", "replace")
    if re.search(r"Client Challenge|captcha|access denied|verify you are human", page, re.I):
        return "non", 0, final_url, "challenge anti-bot"
    extractor = _ArticleParser()
    extractor.feed(page)
    length = len("\n".join(extractor.paragraphs))
    return ("oui" if length >= 400 else "non"), length, final_url, ("" if length >= 400 else "<article> insuffisant")


def diagnose(candidate: Candidate, now: dt.datetime) -> dict:
    row = dict(source=candidate.name, rubrique=candidate.categories, url=candidate.url,
               rss_ok="non", recent_7j=0, recent_48h=0, entries=0, dated=0,
               rss_max=0, rss_median=0, rich_field="—", encoded=0, runtime="—",
               article="non", article_len=0,
               final_url=candidate.url, documentary_len=0, over_900="non",
               recommendation="inutilisable actuellement", reason="")
    code, final, kind, body, error = fetch(candidate.url)
    row["final_url"] = final
    if code != 200 or error:
        row["reason"] = f"HTTP {code} {error}".strip()
        return row
    if candidate.kind == "page":
        if candidate.sample_url:
            row["article"], row["article_len"], row["final_url"], reason = article_probe(candidate.sample_url)
            row["documentary_len"] = min(row["article_len"], 12_000)
            row["over_900"] = "oui" if row["documentary_len"] >= 900 else "non"
            row["reason"] = "page HTML, sans flux/API ; adaptateur de découverte requis" + (f" ; {reason}" if reason else "")
        else:
            row["reason"] = "page HTML, sans flux/API ; adaptateur de découverte requis"
        return row
    if candidate.kind == "api":
        try:
            data = json.loads(body)
            entries = data.get("data") or data.get("release-groups") or []
            row["rss_ok"] = "oui (API)"
            row["entries"] = len(entries)
            row["reason"] = "API JSON non intégrée au collecteur RSS ; adaptateur requis"
        except (ValueError, TypeError, AttributeError):
            row["reason"] = "réponse API non JSON"
        return row
    try:
        _payload(candidate.url)
        row["runtime"] = "oui"
    except Exception as exc:
        row["runtime"] = f"non ({type(exc).__name__})"
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        row["reason"] = f"réponse non RSS/Atom ({kind})"
        return row
    nodes = [node for node in root.iter() if _tag(node) in {"item", "entry"}]
    row["rss_ok"] = "oui"
    row["entries"] = len(nodes)
    samples = []
    for node in nodes:
        when = _date(node)
        if when:
            row["dated"] += 1
            age = now - when.astimezone(dt.timezone.utc)
            if dt.timedelta(0) <= age <= dt.timedelta(days=7):
                row["recent_7j"] += 1
            if dt.timedelta(0) <= age <= dt.timedelta(hours=48):
                row["recent_48h"] += 1
        fields = feed_texts(node)
        if "encoded" in fields or "content" in fields:
            row["encoded"] += 1
        link = _link(node)
        if link:
            best_field = max(fields, key=lambda key: len(fields[key])) if fields else "—"
            samples.append((len(fields[best_field]) if fields else 0, best_field, link, when))
    if samples:
        sizes = sorted(item[0] for item in samples)
        row["rss_max"] = sizes[-1]
        row["rss_median"] = sizes[len(sizes) // 2]
        representative = next((s for s in samples if s[3] and now - s[3].astimezone(dt.timezone.utc) <= dt.timedelta(days=7)), samples[0])
        row["rich_field"] = representative[1]
        row["article"], row["article_len"], row["final_url"], reason = article_probe(representative[2])
        row["reason"] = reason
        row["documentary_len"] = max(min(representative[0], 12_000), min(row["article_len"], 12_000))
        row["over_900"] = "oui" if row["documentary_len"] >= 900 else "non"
        if row["runtime"] != "oui":
            row["reason"] = f'collecteur Python: {row["runtime"]}; {row["reason"]}'.rstrip("; ")
        elif row["over_900"] == "oui":
            row["recommendation"] = "source documentaire utilisable"
        else:
            row["recommendation"] = "source de découverte seulement"
        if row["dated"] == 0:
            row["recommendation"] = "inutilisable actuellement"
            row["reason"] = (row["reason"] + "; " if row["reason"] else "") + "dates absentes : entrées rejetées en génération quotidienne"
    else:
        row["reason"] = "aucune entrée avec texte et lien exploitables"
    return row


def markdown(rows: list[dict], now: dt.datetime) -> str:
    lines = [f"# Diagnostic des sources — {now:%Y-%m-%d %H:%M} UTC", "",
             "Mesure locale, un seul article échantillonné par flux. Récent = 7 jours ; la génération actuelle exige 48 h. ",
             "Longueur = caractères du meilleur contenu RSS/API ou du texte extrait par le parseur actuel, sans addition de sources.",
             "HTTP public et extraction HTML : curl avec validation TLS Windows. Collecteur Python : urllib, testé séparément.",
             "Une réponse HTTPS/HTML ne prouve pas que le contenu est réutilisable ; respecter robots.txt et les licences.", "",
             "| Source | Rubrique | RSS/API OK | Python OK | Entrées 7j/48h (total) | Dates | content:encoded/content | Champ riche | RSS max/méd. | Article | Texte article | Matière | ≥900 | Recommandation | Motif | URL finale |",
             "|---|---|---|---|---:|---:|---:|---|---:|---|---:|---:|---|---|---|---|"]
    for row in rows:
        values = [row["source"], row["rubrique"], row["rss_ok"], row["runtime"],
                  f'{row["recent_7j"]}/{row["recent_48h"]} ({row["entries"]})', row["dated"], row["encoded"],
                  row["rich_field"], f'{row["rss_max"]}/{row["rss_median"]}', row["article"],
                  row["article_len"], row["documentary_len"], row["over_900"],
                  row["recommendation"], row["reason"] or "—", row["final_url"]]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write Markdown report to this local file")
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for candidate in CANDIDATES:
        row = diagnose(candidate, now)
        rows.append(row)
        print(f'{candidate.name}: {row["recommendation"]} ({row["reason"] or row["documentary_len"]})', file=sys.stderr)
    report = markdown(rows, now)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
