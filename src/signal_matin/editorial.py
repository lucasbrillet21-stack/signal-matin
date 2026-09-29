"""Sélection éditoriale du profil personnel, pilotée par config.yaml."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from .config import setting
from .models import FeatureArticle, NewsItem
from .source_material import Material, article_material, wikipedia_material
from .synthesis import compose_feature, llm_configured

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIERS = ("dossier", "article", "lecture")
DEFAULT_TARGETS = {"dossier": (700, 900), "article": (450, 600), "lecture": (300, 450)}


def local_date(config: dict, now: dt.datetime) -> dt.date:
    zone = ZoneInfo(str(setting(config, "editorial.timezone", "Australia/Sydney")))
    return now.astimezone(zone).date()


def categories_for_date(config: dict, date: dt.date) -> list[str]:
    rotation = setting(config, "editorial.rotation", {}) or {}
    if set(rotation) != set(WEEKDAYS):
        raise ValueError("editorial.rotation doit définir les sept jours de la semaine")
    for day, categories in rotation.items():
        if not isinstance(categories, list) or not 1 <= len(categories) <= 3:
            raise ValueError(f"Rotation invalide pour {day} : une à trois rubriques requises")
        if any(not isinstance(value, str) or not value.strip() for value in categories):
            raise ValueError(f"Rubrique vide pour {day}")
        if len(set(categories)) != len(categories):
            raise ValueError(f"Rubrique répétée pour {day}")
    return rotation[WEEKDAYS[date.weekday()]]


def targets(config: dict, tier: str) -> tuple[int, int]:
    values = setting(config, f"editorial.targets.{tier}", DEFAULT_TARGETS[tier])
    if not isinstance(values, list | tuple) or len(values) != 2:
        raise ValueError(f"Cible éditoriale invalide : {tier}")
    lower, upper = map(int, values)
    if not 100 <= lower <= upper <= 1200:
        raise ValueError(f"Cible éditoriale invalide : {tier}")
    return lower, upper


def _tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"\w+", value.casefold()) if len(word) >= 4}


def _related(first: NewsItem, other: NewsItem) -> bool:
    left, right = _tokens(first.title), _tokens(other.title)
    return bool(left and right and len(left & right) / min(len(left), len(right)) >= 0.3)


def _evergreen(config: dict, category: str, date: dt.date) -> Material | None:
    topics = setting(config, f"editorial.evergreen.{category}", []) or []
    if not topics:
        return None
    for offset in range(len(topics)):
        topic = topics[(date.toordinal() + offset) % len(topics)]
        material = wikipedia_material(str(topic))
        if material:
            return material
    return None


def write_features(
    config: dict, date: dt.date, selected: list[str], items: list[NewsItem],
) -> list[FeatureArticle]:
    if not bool(setting(config, "synthesis.enabled", False)) or not llm_configured():
        return []
    features: list[FeatureArticle] = []
    for category in selected:
        tier = TIERS[len(features)]
        candidates = [item for item in items if item.category == category]
        materials: list[Material] = []
        if candidates:
            keywords = [str(word).casefold() for word in
                        (setting(config, "interests.music_keywords", []) or [])]
            def score(item: NewsItem) -> float:
                corroboration = sum(_related(item, other) for other in candidates
                                    if other is not item)
                interest = (category == "Musique" and any(
                    word in f"{item.title} {item.summary}".casefold() for word in keywords
                ))
                return 5 * corroboration + 2 * interest + min(len(item.summary), 300) / 300

            first = max(candidates, key=score)
            related = [item for item in candidates[1:] if _related(first, item)]
            for item in [first, *related[:2]]:
                materials.append(article_material(item))
        if sum(len(material.text) for material in materials) < 900:
            evergreen = _evergreen(config, category, date)
            if evergreen:
                materials = [evergreen]
        if sum(len(material.text) for material in materials) < 900:
            continue
        try:
            feature = compose_feature(category, tier, materials, targets(config, tier))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            feature = None
        if feature:
            features.append(feature)
    return features
