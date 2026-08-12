"""OpenAI-backed semantic assessment plus deterministic domain thresholds."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, Field

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - surfaced through readiness in the UI
    OpenAI = None


APP_DIR = Path(__file__).resolve().parent
PROMPT_FILE = APP_DIR / "domain_drop_prompt.txt"
ANCHOR_PROMPT_FILE = APP_DIR / "domain_anchor_prompt.txt"
SCREEN_PROMPT_FILE = APP_DIR / "domain_screen_prompt.txt"
ARTICLE_PROMPT_FILE = APP_DIR / "domain_article_prompt.txt"


def _env_value(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None and str(value).strip():
        return str(value)
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                registry_value, _ = winreg.QueryValueEx(key, name)
            if registry_value is not None:
                return str(registry_value)
        except (OSError, ImportError):
            pass
    return default


def _env_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(_env_value(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


class LinkQuality(str, Enum):
    QUALITY = "QUALITY"
    SPAM = "SPAM"
    UNCERTAIN = "UNCERTAIN"


class LinkType(str, Enum):
    ARTICLE = "ARTICLE"
    DIRECTORY = "DIRECTORY"
    AUTO = "AUTO"
    PROFILE = "PROFILE"
    OTHER = "OTHER"


class ProhibitedTopic(str, Enum):
    NONE = "NONE"
    CASINO = "CASINO"
    CRYPTO_SCAM = "CRYPTO_SCAM"
    PHARMA = "PHARMA"
    ADULT = "ADULT"
    SPAM = "SPAM"
    DOORWAY = "DOORWAY"


class RiskLevel(str, Enum):
    CLEAN = "CLEAN"
    RISK = "RISK"
    SPAM = "SPAM"


class AgeSignal(str, Enum):
    OLD = "OLD_2015_OR_EARLIER"
    BORDERLINE = "BORDERLINE_2016"
    NORMAL = "NORMAL_2017_PLUS"
    FRESH = "FRESH_2024_2026"
    UNKNOWN = "UNKNOWN"


class LinkAssessment(BaseModel):
    record_id: str
    quality: LinkQuality
    link_type: LinkType
    count_quality: bool
    count_article: bool
    article_weight: float = 1.0
    prohibited_topic: ProhibitedTopic
    age_signal: AgeSignal
    reason: str


class DomainEvidenceAssessment(BaseModel):
    locale: str
    locale_evidence: str = ""
    language: str
    topic: str
    pbn_risk: RiskLevel
    pbn_reasons: List[str]
    anchor_risk: RiskLevel
    anchor_reasons: List[str]
    hard_stop_reasons: List[str]
    link_assessments: List[LinkAssessment]
    summary: str
    warnings: List[str]


class AnchorScreenAssessment(BaseModel):
    """Small first-stage response used before backlink classification."""

    locale: str
    locale_evidence: str = ""
    language: str
    topic: str
    anchor_risk: RiskLevel
    anchor_reasons: List[str]
    hard_stop_reasons: List[str]
    summary: str
    warnings: List[str]


class CriticalScreenAssessment(BaseModel):
    """Cheap screen response; only confident critical stops may end the cascade."""

    anchor_risk: RiskLevel
    pbn_risk: RiskLevel
    hard_stop_reasons: List[str]


class LinkBatchAssessment(BaseModel):
    """Token-light response: the model returns ID sets instead of one object per link."""

    pbn_risk: RiskLevel
    pbn_reasons: List[str]
    hard_stop_reasons: List[str]
    quality_record_ids: List[str]
    article_record_ids: List[str]
    half_article_record_ids: List[str] = Field(default_factory=list)
    old_record_ids: List[str]
    modern_record_ids: List[str]
    borderline_record_ids: List[str]
    fresh_record_ids: List[str]
    unknown_age_record_ids: List[str]
    spam_record_ids: List[str]


class ArticleFallbackAssessment(BaseModel):
    """Small browser-fallback response: only newly confirmed article IDs."""

    article_record_ids: List[str]
    half_article_record_ids: List[str] = Field(default_factory=list)


class FirstBatchAssessment(BaseModel):
    """Anchor screen and first backlink batch in one call to avoid gateway overhead."""

    locale: str
    locale_evidence: str = ""
    language: str
    topic: str
    anchor_risk: RiskLevel
    anchor_reasons: List[str]
    pbn_risk: RiskLevel
    pbn_reasons: List[str]
    hard_stop_reasons: List[str]
    quality_record_ids: List[str]
    article_record_ids: List[str]
    half_article_record_ids: List[str] = Field(default_factory=list)
    old_record_ids: List[str]
    modern_record_ids: List[str]
    borderline_record_ids: List[str]
    fresh_record_ids: List[str]
    unknown_age_record_ids: List[str]
    spam_record_ids: List[str]


@dataclass
class DomainVerdict:
    verdict: str
    status: str
    reason: str
    locale: str = ""
    language: str = ""
    topic: str = ""
    unique_quality: int = 0
    article_links: float = 0
    homepage_links: int = 0
    old_links: int = 0
    modern_links: int = 0
    unknown_age_links: int = 0
    link_year_min: int = 0
    link_year_max: int = 0
    link_year_count: int = 0
    required_unique: int = 0
    required_articles: int = 0
    unique_deficit: int = 0
    article_deficit: float = 0
    anchor_risk: str = ""
    locale_source: str = ""
    hard_stop_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    backlinks_sent: int = 0
    anchors_sent: int = 0
    early_stop_stage: str = ""
    webarchive_status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value or ""))


def _syndicated_content_key(row: Dict[str, Any]) -> str:
    """Collapse mirrored/syndicated copies of the same article across city/info portals."""

    raw_url = str(row.get("source_url") or "").strip()
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return ""
    path = unquote(re.sub(r"/{2,}", "/", parsed.path or "/")).lower().rstrip("/")
    if not path or path == "/":
        return ""
    slug = path.rsplit("/", 1)[-1]
    slug_tokens = [token for token in re.split(r"[^a-z0-9]+", slug) if len(token) >= 3]
    has_specific_date = bool(re.search(r"(?:19|20)\d{2}[-_/]\d{2}[-_/]\d{2}", path))
    has_long_slug = len(slug) >= 48 and len(slug_tokens) >= 5
    if not (has_specific_date or has_long_slug):
        return ""
    # Ignore protocol, host and query: syndicated portals often publish the
    # same article under different regional domains but with the same path.
    return f"syndicated|{path}"


def _canonical_source_key(row: Dict[str, Any]) -> str:
    syndicated_key = _syndicated_content_key(row)
    if syndicated_key:
        return syndicated_key
    domain = str(row.get("source_domain") or "").lower().removeprefix("www.")
    raw_url = str(row.get("source_url") or "").strip()
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or domain).lower().removeprefix("www.")
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        normalized_url = f"{host}{path}" + (f"?{query}" if query else "")
    except ValueError:
        normalized_url = raw_url.lower().rstrip("/")
    return f"{domain}|{normalized_url}"


def is_exact_homepage(target_url: str, domain: str) -> bool:
    raw = str(target_url or "").strip()
    if raw == "/":
        return True
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    expected = str(domain or "").lower().removeprefix("www.")
    return host == expected and (parsed.path or "/") == "/" and not parsed.params and not parsed.query and not parsed.fragment


def extract_explicit_years(*values: Any) -> List[int]:
    """Extract visible four-digit years without treating Majestic crawl dates as link age.

    Years before 2000 are ignored for backlink freshness: in donor titles/URLs
    they are too often event, birth/founding, archive, or product/model years
    rather than publication/link-placement dates.
    """

    years: List[int] = []
    for value in values:
        text = str(value or "")
        if not text:
            continue
        candidates = [text]
        if "%" in text:
            try:
                candidates.append(unquote(text))
            except Exception:
                pass
        for candidate in candidates:
            for match in YEAR_RE.finditer(candidate):
                try:
                    year = int(match.group(0))
                except ValueError:
                    continue
                if MIN_LINK_CONTEXT_YEAR <= year <= 2026:
                    years.append(year)
    return sorted(set(years))


def backlink_source_years(row: Dict[str, Any]) -> List[int]:
    """Years from donor URL/title and fetched donor-page title/description only."""

    page_years = row.get("page_years")
    explicit_page_years: List[int] = []
    if isinstance(page_years, list):
        for value in page_years:
            try:
                explicit_page_years.append(int(value))
            except (TypeError, ValueError):
                continue
    return sorted(
        set(
            extract_explicit_years(
                row.get("source_url"),
                row.get("source_title"),
            )
            + [year for year in explicit_page_years if MIN_LINK_CONTEXT_YEAR <= year <= 2026]
        )
    )


def backlink_age_bucket(row: Dict[str, Any], cutoff_year: int = 2016) -> str:
    """Classify backlink age without spending AI tokens.

    Only visible donor URL/title/page years are used. Majestic First Indexed and
    Last Seen are intentionally ignored because they can refresh/reset and do
    not reliably represent the real placement date.
    """

    cutoff = max(1900, min(int(cutoff_year or 2016), 2030))
    source_years = backlink_source_years(row)
    if source_years:
        return "old" if max(source_years) < cutoff else "modern"
    return "unknown"


def clean_freshness_cutoff_year(value: Any, fallback: int = 2016) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(1990, min(parsed, 2030))


def clean_freshness_old_share_percent(value: Any, fallback: int = 50) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(0, min(parsed, 100))


def _valid_locale_code(value: Any) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if re.fullmatch(r"[A-Z]{2}", candidate) else ""


def batch_locale_override(title: str) -> str:
    value = str(title or "").strip()
    match = re.match(r"^(?:!|locale\s*[:=]\s*|loc\s*[:=]\s*)([A-Za-z]{2})(?:\b|[^A-Za-z])", value, re.IGNORECASE)
    return _valid_locale_code(match.group(1)) if match else ""


def tld_locale(domain: str) -> str:
    tld = str(domain or "").lower().rstrip(".").rsplit(".", 1)[-1].upper()
    return _valid_locale_code(tld)


LOCALE_GEO_PATTERNS: Dict[str, re.Pattern[str]] = {
    "AT": re.compile(
        r"\b(?:austria|austrian|vienna|wien|oesterreich|osterreich|\u00f6sterreich|graz|salzburg|linz|innsbruck)\b",
        re.IGNORECASE,
    ),
    "CH": re.compile(
        r"\b(?:switzerland|swiss|schweiz|suisse|svizzera|zurich|z\u00fcrich|geneva|gen\u00e8ve|geneve|lausanne|bern|basel|lugano|winterthur)\b",
        re.IGNORECASE,
    ),
    "DE": re.compile(
        r"\b(?:germany|german|deutschland|berlin|muenchen|munich|hamburg|frankfurt|stuttgart|koeln|k\u00f6ln|cologne|duesseldorf|d\u00fcsseldorf|leipzig|dresden|hannover|nuremberg|nuernberg)\b",
        re.IGNORECASE,
    ),
    "FR": re.compile(r"\b(?:france|french|paris|lyon|marseille|toulouse|nice|nantes|bordeaux|lille)\b", re.IGNORECASE),
    "IT": re.compile(r"\b(?:italy|italia|italian|roma|rome|milano|milan|torino|turin|napoli|florence|firenze|bologna)\b", re.IGNORECASE),
    "ES": re.compile(r"\b(?:spain|spanish|espa\u00f1a|espana|madrid|barcelona|valencia|sevilla|seville|zaragoza)\b", re.IGNORECASE),
    "PL": re.compile(r"\b(?:poland|polish|polska|warszawa|warsaw|krakow|krak\u00f3w|wroclaw|wroc\u0142aw|poznan|pozna\u0144|gdansk|gda\u0144sk)\b", re.IGNORECASE),
    "NL": re.compile(r"\b(?:netherlands|dutch|nederland|amsterdam|rotterdam|utrecht|eindhoven|den haag|the hague)\b", re.IGNORECASE),
    "BE": re.compile(r"\b(?:belgium|belgian|belgique|belgi\u00eb|brussels|bruxelles|brussel|antwerp|antwerpen|gent|ghent)\b", re.IGNORECASE),
    "CA": re.compile(r"\b(?:canada|canadian|toronto|vancouver|montreal|montr\u00e9al|ottawa|calgary|quebec|qu\u00e9bec)\b", re.IGNORECASE),
    "AU": re.compile(r"\b(?:australia|australian|sydney|melbourne|brisbane|perth|adelaide|canberra)\b", re.IGNORECASE),
    "BR": re.compile(r"\b(?:brazil|brasil|brazilian|s\u00e3o paulo|sao paulo|rio de janeiro|brasilia|bras\u00edlia)\b", re.IGNORECASE),
    "CL": re.compile(r"\b(?:chile|chilean|santiago|valparaiso|valpara\u00edso)\b", re.IGNORECASE),
    "PE": re.compile(r"\b(?:peru|peruvian|per\u00fa|lima|arequipa|cusco)\b", re.IGNORECASE),
    "PY": re.compile(r"\b(?:paraguay|paraguayan|asuncion|asunci\u00f3n)\b", re.IGNORECASE),
    "HU": re.compile(r"\b(?:hungary|hungarian|magyar|budapest|debrecen|szeged)\b", re.IGNORECASE),
}

LOCALE_TLDS = set(LOCALE_GEO_PATTERNS) | {"LT", "LV", "EE", "SE", "IN", "IE", "UK", "US"}


def _host_tld(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE) else f"http://{raw}"
    try:
        host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    tld = host.rsplit(".", 1)[-1].upper() if "." in host else ""
    return tld if re.fullmatch(r"[A-Z]{2}", tld) else ""


def infer_locale_from_backlinks_with_source(backlinks: Iterable[Dict[str, Any]]) -> tuple[str, str]:
    """Conservative locale fallback when AI leaves a generic-TLD domain as OTHER.

    Country inference needs strong geo evidence or dominant country ccTLD donors.
    English language without clear country evidence becomes EN, an international
    English bucket, not US/UK/AU by guess.
    """

    rows = list(backlinks)
    scores: Counter[str] = Counter()
    tld_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    for row in rows:
        for field_name in ("source_domain", "source_url"):
            tld = _host_tld(row.get(field_name))
            if tld in LOCALE_TLDS:
                tld_counts[tld] += 1
                if tld in LOCALE_GEO_PATTERNS:
                    scores[tld] += 1.5
                break
        language = _valid_locale_code(row.get("language"))
        if language:
            language_counts[language] += 1
        text = " ".join(
            str(row.get(name) or "")
            for name in (
                "source_url",
                "source_title",
                "source_anchor_text",
                "anchor",
                "target_title",
                "target_url",
            )
        )
        for locale, pattern in LOCALE_GEO_PATTERNS.items():
            if pattern.search(text):
                scores[locale] += 4

    if scores:
        winner, winner_score = scores.most_common(1)[0]
        runner_score = scores.most_common(2)[1][1] if len(scores) > 1 else 0
        if winner_score >= 5 and winner_score >= runner_score + 2:
            return winner, "BACKLINKS"

    total_known_tlds = sum(tld_counts.values())
    if total_known_tlds:
        winner, count = tld_counts.most_common(1)[0]
        if winner in LOCALE_GEO_PATTERNS and count >= 5 and count / total_known_tlds >= 0.75:
            return winner, "BACKLINKS"

    total_languages = sum(language_counts.values())
    english_count = language_counts.get("EN", 0)
    if total_languages and english_count >= 3 and english_count / total_languages >= 0.60:
        return "EN", "LANGUAGE"
    if len(rows) >= 3 and not total_languages:
        englishish = sum(
            1
            for row in rows
            if re.search(
                r"\b(?:about|news|blog|article|podcast|review|interview|event|press|website|official)\b",
                " ".join(str(row.get(name) or "") for name in ("source_url", "source_title", "anchor", "source_anchor_text", "target_title")),
                re.IGNORECASE,
            )
        )
        if englishish >= 3:
            return "EN", "LANGUAGE"
    return "", ""


def infer_locale_from_backlinks(backlinks: Iterable[Dict[str, Any]]) -> str:
    return infer_locale_from_backlinks_with_source(backlinks)[0]


def resolve_locale_with_source(title: str, domain: str, ai_locale: str) -> tuple[str, str]:
    manual = batch_locale_override(title)
    if manual:
        return manual, "OVERRIDE"
    ai_value = _valid_locale_code(ai_locale)
    if ai_value:
        return ai_value, "AI"
    tld_value = tld_locale(domain)
    if tld_value:
        return tld_value, "TLD"
    return "OTHER", "FALLBACK"


def resolve_locale(title: str, domain: str, ai_locale: str) -> str:
    return resolve_locale_with_source(title, domain, ai_locale)[0]


def thresholds_for_locale(locale: str) -> tuple[int, int]:
    code = str(locale or "").upper()
    if code == "EN":
        return 9, 5
    if code in {"LT", "LV", "EE"}:
        return 7, 0
    if code in {"BR", "PL", "SE", "CL", "IN", "AU"}:
        return 7, 3
    return 9, 5


def _deficit_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(parsed, 50))


def near_deficit_limits(
    strict_mode: bool = False,
    strict_unique_deficit: int = 1,
    strict_article_deficit: int = 1,
) -> tuple[int, int]:
    """Allowed deficit for GOOD (NEAR THRESHOLD)."""

    if strict_mode:
        return _deficit_value(strict_unique_deficit, 1), _deficit_value(strict_article_deficit, 1)
    return 3, 2


def near_thresholds_for_locale(
    locale: str,
    strict_mode: bool = False,
    strict_unique_deficit: int = 1,
    strict_article_deficit: int = 1,
) -> tuple[int, int]:
    required_unique, required_articles = thresholds_for_locale(locale)
    unique_margin, article_margin = near_deficit_limits(
        strict_mode,
        strict_unique_deficit,
        strict_article_deficit,
    )
    return max(0, required_unique - unique_margin), max(0, required_articles - article_margin)


DOMAIN_NAME_HARD_STOP_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "pharma",
        re.compile(
            r"(?:"
            r"(?:^|-)(?:cialis|viagra|levitra|kamagra)(?:-|$)|"
            r"(?:cheap|buy|online|best|fast|generic|rx|my)(?:cialis|viagra|levitra|kamagra)|"
            r"(?:cialis|viagra|levitra|kamagra)(?:cheap|buy|online|here|fast|generic|rx|ok)|"
            r"viagra|levitra|kamagra|sildenafil|tadalafil|phentermine|provigil|priligy|"
            r"onlinepharmacy|pharmacy|pharma"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "casino/betting",
        re.compile(
            r"(?:casino|kasino|gambling|betting|sportsbook|bookmaker|"
            r"bet365|1xbet|22bet|20bet|melbet|dafabet|pinupbet|kubet|kbet|"
            r"\d+bet|bet\d+|slots|slotgame|slotgacor|poker|togel|"
            r"cakep-?togel|judi-?online|judi-?(?:bola|slot)|"
            r"situs-?(?:judi|slot|togel)|bandar-?(?:judi|togel)|agen-?(?:bola|togel)|"
            r"togel-?online|slot-?gacor)",
            re.IGNORECASE,
        ),
    ),
    (
        "adult",
        re.compile(
            r"(?:porn|porno|xxx|hentai|escort|adultdating|sexcam|webcamsex|onlyfans|"
            r"erotikads|eroticmassage|erotischemassage|sexspielzeug)",
            re.IGNORECASE,
        ),
    ),
    (
        "crypto/nft",
        re.compile(
            r"(?:"
            r"crypto(?!graphy)|cryptocurrency|bitcoin|bitcoins|(?:^|-)btc(?:-|$)|btc(?:coin|casino|market|wallet|trading)|"
            r"blockchain|ethereum|litecoin|dogecoin|altcoin|memecoin|(?:^|-)defi(?:-|$)|"
            r"airdrop|binance|coinbase|web3|metaverse|"
            r"(?:coin|token)(?:market|wallet|trading|swap|sale|airdrop)|"
            r"(?:^|-)nfts?(?:-|$)|(?:^|-)nft(?:coin|token|market|art|game|games)(?:-|$)?|"
            r"nft(?:coin|token|market|art|game|games)|(?:coin|token|market|art|game|games)nft"
            r")",
            re.IGNORECASE,
        ),
    ),
    ("forex/trading", re.compile(r"(?:forex|binaryoptions|binary-options|metatrader|mt4|mt5|tradingsignals|trading-signals)", re.IGNORECASE)),
    ("loans", re.compile(r"(?:paydayloans?|payday-loans?|quickloans?|quick-loans?|cashloans?|cash-loans?)", re.IGNORECASE)),
    (
        "counterfeit/cheap goods",
        re.compile(
            r"(?:"
            r"(?:cheap|replica|wholesale|discount|sale|buy|fake|outlet|onlinesale).{0,30}"
            r"(?:jerseys?|airjordan|jordans?|oakleys?|rayban|louisvuitton|gucci|prada|"
            r"nike|adidas|yeezy|sneakers?|shoes?|handbags?|bags?|watches?|rolex|"
            r"michaelkors|burberry|coach|pandora|tiffany|moncler|northface|canadagoose|"
            r"ugg|timberland|beatsbydre|mulberry|sunglasses|jackets?)|"
            r"(?:jerseys?|airjordan|jordans?|oakleys?|rayban|louisvuitton|gucci|prada|"
            r"nike|adidas|yeezy|sneakers?|shoes?|handbags?|bags?|watches?|rolex|"
            r"michaelkors|burberry|coach|pandora|tiffany|moncler|northface|canadagoose|"
            r"ugg|timberland|beatsbydre|mulberry|sunglasses|jackets?).{0,30}"
            r"(?:cheap|replica|wholesale|discount|sale|buy|fake|outlet|onlinesale)|"
            r"(?:louisvuitton|gucci|prada|rolex|michaelkors|burberry|coach|pandora|"
            r"tiffany|moncler|northface|canadagoose|ugg|beatsbydre|mulberry).{0,20}"
            r"(?:bags?|handbags?|watches?|jackets?|shoes?|sunglasses?|outlet|sale|cheap|replica)|"
            r"(?:nfl|nba|nhl|mlb|soccer|football).{0,20}jerseys?|"
            r"jerseys?.{0,20}(?:nfl|nba|nhl|mlb|soccer|football)"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "academic writing spam",
        re.compile(
            r"(?:"
            r"(?:buy|cheap|custom|best|write|writing|online).{0,24}"
            r"(?:essays?|dissertations?|assignments?|homework|researchpapers?)|"
            r"(?:essays?|dissertations?|assignments?|homework|researchpapers?).{0,24}"
            r"(?:help|writer|writers|writing|service|services|online|cheap|buy|papers?)|"
            r"essaywriting|assignmenthelp|dissertationhelp|homeworkhelp"
            r")",
            re.IGNORECASE,
        ),
    ),
    ("doorway", re.compile(r"(?:doorway|dorway)", re.IGNORECASE)),
    ("exam dumps", re.compile(r"(?:braindumps|examdumps|exam-dumps|testking|pdfvce|newdumpspdf|itdumps|topexam)", re.IGNORECASE)),
    ("seo/link spam", re.compile(r"(?:buybacklinks|buy-backlinks|seobacklinks|seo-backlinks|ageddomains|aged-domains|expireddomains|expired-domains|pbnlinks|pbn-links)", re.IGNORECASE)),
)


def local_domain_name_precheck(domain: str, title: str = "") -> Optional[DomainVerdict]:
    """Reject domains whose own name is an unambiguous prohibited topic."""

    clean_domain = str(domain or "").strip().lower().removeprefix("www.")
    if not clean_domain:
        return None
    parts = [part for part in clean_domain.split(".") if part]
    two_part_suffixes = {
        "co.uk",
        "com.au",
        "net.au",
        "org.au",
        "com.br",
        "com.tr",
        "co.in",
    }
    if len(parts) >= 3 and ".".join(parts[-2:]) in two_part_suffixes:
        sld = parts[-3]
    elif len(parts) >= 2:
        sld = parts[-2]
    else:
        sld = parts[0] if parts else clean_domain
    normalized = re.sub(r"[^a-z0-9]+", "-", sld)
    compact = re.sub(r"[^a-z0-9]+", "", sld)
    haystacks = [normalized, compact]
    for label, pattern in DOMAIN_NAME_HARD_STOP_PATTERNS:
        if any(pattern.search(value) for value in haystacks):
            locale, locale_source = resolve_locale_with_source(title, clean_domain, "")
            required_unique, required_articles = thresholds_for_locale(locale)
            reason = (
                f"Локальный стоп до Majestic/API: запрещенная тема уже в имени домена "
                f"({label}); домен «{clean_domain}»."
            )
            return DomainVerdict(
                verdict="REJECT",
                status="BAD:DOMAIN_NAME",
                reason=reason,
                locale=locale,
                locale_source=locale_source,
                required_unique=required_unique,
                required_articles=required_articles,
                hard_stop_reasons=[reason],
                model="LOCAL_RULES",
                early_stop_stage="local_domain_name",
            )
    return None


ANCHOR_HARD_STOP_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "casino/betting",
        re.compile(
            r"(?<![\w])(?:casino|kasino|казино|gambling|betting|sportsbook|букмекер|ставки\s+на\s+спорт|"
            r"togel|judi\s+online|judi\s+(?:bola|slot)|situs\s+(?:judi|slot|togel)|"
            r"bandar\s+(?:judi|togel)|agen\s+(?:bola|togel)|slot\s+gacor|cakep\s*togel|"
            r"(?:deposit\s+pulsa.{0,40}(?:slot|togel|judi)|(?:slot|togel|judi).{0,40}deposit\s+pulsa))(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "pharma",
        re.compile(
            r"(?<![\w])(?:viagra|cialis|levitra|kamagra|виагра|сиалис|online\s+pharmacy)(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "adult",
        re.compile(
            r"(?<![\w])(?:porn|porno|xxx|порно|hentai|escort\s+girls?|erotikads|"
            r"erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|"
            r"sexspielzeug(?:e|en)?|sexuelle\s+fantasien|naked\s+body)(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "crypto/investment scam",
        re.compile(
            r"(?<![\w])(?:crypto\s+casino|bitcoin\s+casino|guaranteed\s+crypto\s+profit|"
            r"bitcoin\s+investment|инвестиции\s+без\s+риска|крипто\s+казино)(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "forex/trading",
        re.compile(
            r"(?:\bforex\b|\bfx\b|fxと外為|外為|外国為替|為替|"
            r"foreign\s+exchange|devisenhandel|форекс|"
            r"\b(?:binary\s+options?|cfd|metatrader|mt[45])\b|"
            r"\b(?:forex|fx)\s+(?:broker|brokers|trading|signals?|robot|bonus)\b|"
            r"\b(?:trading\s+signals?|online\s+trading)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "doorway",
        re.compile(
            r"(?<![\w])(?:doorway|дорвей|дорвеи)(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "exam/certification dumps",
        re.compile(
            r"(?:\b(?:exam|certification|certificate|practice|valid|real|latest)\s+"
            r"(?:dumps?|braindumps?|questions?|sims?|test|guide|pdf)\b|"
            r"\b(?:dumps?|braindumps?|testking|pdfvce|itdumpskr|newdumpspdf|itzert|topexam)\b|"
            r"(?:pr.fungsfragen|pr.fungsunterlagen|fragen\s+und\s+antworten|fragenkatalog|"
            r"시험|덤프|考題|題庫|資格考試))",
            re.IGNORECASE,
        ),
    ),
)
ANCHOR_SINGLE_ROW_HARD_STOP_LABELS = frozenset({"forex/trading"})


def unique_backlinks(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one row per canonical donor-page pair before spending API tokens."""

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = _canonical_source_key(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def scan_anchor_hard_stops(
    fresh_anchors: Dict[str, Any],
    historic_anchors: Dict[str, Any],
) -> List[str]:
    """Find only unambiguous stop phrases locally; semantic ambiguity stays with AI."""

    # Fresh and Historic substantially overlap. Collapse identical anchors and
    # require several distinct bad anchors or meaningful referring-domain
    # weight; one isolated bad row must never reject a good domain.
    profile: Dict[str, Dict[str, Any]] = {}
    for index_name, report in (("Fresh", fresh_anchors), ("Historic", historic_anchors)):
        for row in report.get("rows", []):
            anchor = str(row.get("anchor") or "").strip()
            if not anchor:
                continue
            key = re.sub(r"\s+", " ", anchor).casefold()
            weight = max(
                1,
                int(_number(row.get("referring_domains"))),
                int(_number(row.get("total_links"))),
            )
            current = profile.setdefault(
                key,
                {"anchor": anchor, "weight": 0, "indexes": set()},
            )
            current["weight"] = max(current["weight"], weight)
            current["indexes"].add(index_name)

    total_weight = sum(item["weight"] for item in profile.values()) or 1
    matches: Dict[str, List[Dict[str, Any]]] = {}
    for item in profile.values():
        for label, pattern in ANCHOR_HARD_STOP_PATTERNS:
            if pattern.search(str(item["anchor"])):
                matches.setdefault(label, []).append(item)
                break

    reasons: List[str] = []
    for label, items in matches.items():
        hit_weight = sum(item["weight"] for item in items)
        share = hit_weight / total_weight
        single_row_forbidden = label in ANCHOR_SINGLE_ROW_HARD_STOP_LABELS
        substantial = single_row_forbidden or len(items) >= 2 or hit_weight >= 5
        if not substantial or (not single_row_forbidden and share < 0.10 and hit_weight < 10):
            continue
        example = re.sub(r"\s+", " ", str(items[0]["anchor"]))[:70]
        share_text = "<1%" if 0 < share < 0.01 else f"{share:.0%}"
        index_names = sorted(set().union(*(item["indexes"] for item in items)))
        reasons.append(
            f"{'+'.join(index_names)}: существенный {label} в анкорах "
            f"({len(items)} строк, вес {hit_weight}, {share_text}); пример «{example}»"
        )
        if len(reasons) >= 5:
            break
    return list(dict.fromkeys(reasons))


SEO_NOISE_REASON_RE = re.compile(
    r"(?:\bseo\b|backlinks?|link[ -]?building|\bpbn\b|rankvance|aged domains?|"
    r"expired domains?|покупк\w* ссыл|продаж\w* ссыл|seo[- ]?ссыл|ссылочн\w* сет)",
    re.IGNORECASE,
)
SEO_ANCHOR_NOISE_REASON_RE = re.compile(
    r"(?:autogen\w*|автоген\w*).{0,50}(?:seo|anchors?|анкоры?|backlinks?|authority|link[ -]?building)|"
    r"(?:seo|anchors?|анкоры?|backlinks?|authority|link[ -]?building).{0,50}(?:autogen\w*|автоген\w*)",
    re.IGNORECASE,
)
PROHIBITED_TOPIC_REASON_RE = re.compile(
    r"(?:casino|kasino|казино|betting|gambling|букмек|adult|porn|porno|порно|"
    r"erotikads|erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|sexspielzeug(?:e|en)?|sexuelle\s+fantasien|naked\s+body|"
    r"togel|judi\s+online|judi\s+(?:bola|slot)|situs\s+(?:judi|slot|togel)|bandar\s+(?:judi|togel)|slot\s+gacor|cakep\s*togel|"
    r"pharma|viagra|cialis|levitra|crypto\W*(?:scam|скам|мошен)|"
    r"forex|\bfx\b|foreign\s+exchange|devisenhandel|форекс|binary\s+options?|cfd|metatrader|mt[45]|"
    r"exam\s+(?:dumps?|questions?|sims?)|certification\s+(?:dumps?|questions?)|"
    r"braindumps?|testking|pdfvce|itdumpskr|newdumpspdf|itzert|topexam|"
    r"doorway|дорве)",
    re.IGNORECASE,
)
STRONG_FORBIDDEN_TOPIC_REASON_RE = re.compile(
    r"(?:casino|kasino|казино|betting|gambling|букмек|adult|porn|porno|порно|"
    r"erotikads|erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|sexspielzeug(?:e|en)?|sexuelle\s+fantasien|naked\s+body|"
    r"togel|judi\s+online|judi\s+(?:bola|slot)|situs\s+(?:judi|slot|togel)|bandar\s+(?:judi|togel)|slot\s+gacor|cakep\s*togel|"
    r"pharma|viagra|cialis|levitra|crypto\W*(?:scam|скам|мошен)|"
    r"forex|\bfx\b|foreign\s+exchange|devisenhandel|форекс|binary\s+options?|cfd|metatrader|mt[45]|"
    r"exam\s+(?:dumps?|questions?|sims?)|certification\s+(?:dumps?|questions?)|"
    r"braindumps?|testking|pdfvce|itdumpskr|newdumpspdf|itzert|topexam)",
    re.IGNORECASE,
)
BRAND_NATURAL_SAFE_REASON_RE = re.compile(
    r"(?:brand(?:ed)?|бренд\w*|url|урл|естествен\w*|natural|"
    r"тематическ\w*.{0,35}связ\w*|связн\w*|clean|чист\w*)",
    re.IGNORECASE,
)
AFFILIATE_REPROFILE_REASON_RE = re.compile(
    r"(?:affiliate[- ]?(?:directory|marketing)?|аффил\w*|партн[её]р\w*|topranked|"
    r"gaming\s*&\s*gambling|gambling|betting).{0,160}"
    r"(?:репрофил|перепрофил|переиспольз|redirect|редирект|перенаправ|смен\w*\s+тем|reprofil|repurpos)|"
    r"(?:репрофил|перепрофил|переиспольз|redirect|редирект|перенаправ|смен\w*\s+тем|reprofil|repurpos).{0,160}"
    r"(?:affiliate[- ]?(?:directory|marketing)?|аффил\w*|партн[её]р\w*|topranked|"
    r"gaming\s*&\s*gambling|gambling|betting)",
    re.IGNORECASE,
)
UNVERIFIED_ROOT_REDIRECT_REASON_RE = re.compile(
    r"(?:корень|root|главн\w*|homepage).{0,100}(?:redirect|редирект|перенаправ).{0,140}"
    r"(?:affiliate|аффил|партн[её]р|lander|лендер|директор|directory|/lander)|"
    r"(?:affiliate|аффил|партн[её]р|lander|лендер|директор|directory|/lander).{0,140}"
    r"(?:redirect|редирект|перенаправ|doorway|дорве)",
    re.IGNORECASE,
)
NEGATED_RISK_REASON_RE = re.compile(
    r"(?:нет|без|отсутств|не\s+(?:найден|обнаруж|видн)|no|without|not).{0,80}"
    r"(?:запрещ|спам|spam|pbn|casino|adult|pharma|crypto|doorway|дорве|forex)|"
    r"(?:запрещ|спам|spam|pbn|casino|adult|pharma|crypto|doorway|дорве|forex).{0,80}"
    r"(?:нет|без|отсутств|no|without|not)",
    re.IGNORECASE,
)
INDEPENDENT_CRITICAL_REASON_RE = re.compile(
    r"(?:casino|kasino|казино|betting|gambling|букмек|adult|porn|porno|порно|"
    r"erotikads|erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|sexspielzeug(?:e|en)?|sexuelle\s+fantasien|naked\s+body|"
    r"togel|judi\s+online|judi\s+(?:bola|slot)|situs\s+(?:judi|slot|togel)|bandar\s+(?:judi|togel)|slot\s+gacor|cakep\s*togel|"
    r"競馬|賭け|賭博|カジノ|ブックメーカー|"
    r"pharma|viagra|cialis|levitra|таблет|crypto\W*(?:scam|скам|мошен)|"
    r"forex|\bfx\b|fxと外為|外為|外国為替|為替|foreign\s+exchange|devisenhandel|форекс|"
    r"binary\s+options?|cfd|metatrader|mt[45]|trading\s+signals?|"
    r"exam\s+(?:dumps?|questions?|sims?|test|guide)|certification\s+(?:dumps?|questions?)|"
    r"braindumps?|testking|pdfvce|itdumpskr|newdumpspdf|itzert|topexam|"
    r"крипт\w*\W*(?:скам|мошен)|инвест\w*\W*(?:скам|мошен)|doorway|дорве|"
    r"autogen|автоген|переиспольз|юзан\w*\W+(?:спам|дроп)|"
    r"смен\w*\W+(?:тем|язык)|запрещ[её]н)",
    re.IGNORECASE,
)

HISTORIC_PAGE_FORBIDDEN_RE = re.compile(
    r"(?:casino|kasino|казино|betting|gambling|bookmaker|sportsbook|букмек|"
    r"togel|judi\s+online|judi\s+(?:bola|slot)|situs\s+(?:judi|slot|togel)|bandar\s+(?:judi|togel)|slot\s+gacor|cakep\s*togel|"
    r"(?:deposit\s+pulsa.{0,40}(?:slot|togel|judi)|(?:slot|togel|judi).{0,40}deposit\s+pulsa)|"
    r"競馬|賭け|賭博|カジノ|ブックメーカー|"
    r"adult|porn|porno|порно|erotikads|erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|"
    r"sexspielzeug(?:e|en)?|sexuelle\s+fantasien|sexuelle\s+bed[üu]rfnisse|sexuelle\s+verbindung|naked\s+body|"
    r"viagra|cialis|levitra|(?:crypto|krypto)\s*(?:scam|casino|signals?|signale|trading|investment|investing)|"
    r"forex|\bfx\b|foreign\s+exchange|binary\s+options?|metatrader|"
    r"exam\s+(?:dumps?|questions?|sims?|test|guide)|certification\s+(?:dumps?|questions?)|"
    r"braindumps?|testking|pdfvce|itdumpskr|newdumpspdf|itzert|topexam|"
    r"doorway|дорве|\bslots?\b|\bpoker\b|บาคาร่า)",
    re.IGNORECASE,
)
HISTORIC_PAGE_STRONG_FORBIDDEN_RE = re.compile(
    r"(?:"
    r"(?:^|[/\s_-])(?:crypto|krypto)[/\s_-]*(?:signals?|signale|trading|investment|investing)(?:$|[/\s_-])|"
    r"(?:^|[/\s_-])(?:bitcoin|btc)[/\s_-]*(?:signals?|trading|investment|investing)(?:$|[/\s_-])"
    r")",
    re.IGNORECASE,
)
HISTORIC_PAGE_MEANINGFUL_PATH_RE = re.compile(
    r"/(?:news|blog|article|post|single-post|sponsor|about|contact|get-involved|"
    r"event|events|regatta|race-information|program|schedule|gallery|team|"
    r"press|media|results|registration|entry|entries)(?:/|$)",
    re.IGNORECASE,
)
HISTORIC_PAGE_IGNORE_PATH_RE = re.compile(
    r"/(?:feed\.xml|rss|sitemap(?:\.xml)?|robots\.txt|favicon\.ico)(?:$|\?)",
    re.IGNORECASE,
)
HISTORIC_PAGE_WP_ASSET_HTML_RE = re.compile(
    r"/wp-(?:content|includes)/(?!uploads/)[^?#]*\.html(?:$|\?)",
    re.IGNORECASE,
)
HISTORIC_PAGE_WP_ASSET_RANDOM_RE = re.compile(
    r"(?:^|/)[a-z0-9_-]*(?:-[a-z0-9]{4,})[-_]\d{4,}\.html$",
    re.IGNORECASE,
)
HISTORIC_PAGE_PRODUCT_DOORWAY_RE = re.compile(
    r"(?:\bbaby\b|strollers?|bugg(?:y|ies)|car\s*seats?|poussettes?|reservedelar|pas\s+cher|"
    r"\bcheap\b|discounts?|coupons?|buy\s+online|for\s+sale|price\s+compare|"
    r"replicas?|jerseys?|sneakers?|shoes?|handbags?|watches?)",
    re.IGNORECASE,
)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
WESTERN_LETTER_RE = re.compile(r"[A-Za-z]")
MIN_LINK_CONTEXT_YEAR = 2000
YEAR_RE = re.compile(r"(?<!\d)(?:19\d{2}|20[0-2]\d)(?!\d)")
ANCHOR_PRECHECK_SUSPICIOUS_RE = re.compile(
    r"(?:casino|kasino|gambling|betting|sportsbook|bookmaker|"
    r"togel|judi\s+online|judi\s+(?:bola|slot)|situs\s+(?:judi|slot|togel)|bandar\s+(?:judi|togel)|slot\s+gacor|cakep\s*togel|"
    r"adult|porn|porno|xxx|erotikads|erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|sexspielzeug(?:e|en)?|"
    r"sexuelle\s+fantasien|naked\s+body|viagra|cialis|levitra|pharma|"
    r"forex|\bfx\b|foreign\s+exchange|binary\s+options?|cfd|metatrader|"
    r"exam\s+(?:dumps?|questions?|sims?|test|guide)|certification\s+(?:dumps?|questions?)|"
    r"braindumps?|testking|pdfvce|itdumpskr|newdumpspdf|itzert|topexam|"
    r"doorway|seo\s+backlinks?|buy\s+backlinks?|premium\s+backlinks?|"
    r"aged\s+domains?|expired\s+domains?|rankvance|pbn\s+(?:service|network)|link[ -]?building)",
    re.IGNORECASE,
)
ANCHOR_COMMON_SAFE_RE = re.compile(
    r"^(?:website|webseite|web\s+site|site|homepage|home|official\s+site|"
    r"zur\s+(?:webseite|website|homepage)|visit\s+site|open\s+in\s+new\s+window|"
    r"in\s+neuem\s+fenster\s+öffnen|hier|click|klick|more|read\s+more)$",
    re.IGNORECASE,
)


def _anchor_profile_items(
    fresh_anchors: Dict[str, Any],
    historic_anchors: Dict[str, Any],
) -> List[Dict[str, Any]]:
    profile: Dict[str, Dict[str, Any]] = {}
    for index_name, report in (("Fresh", fresh_anchors), ("Historic", historic_anchors)):
        for row in report.get("rows", []):
            anchor = re.sub(r"\s+", " ", str(row.get("anchor") or "")).strip()
            if not anchor:
                continue
            key = anchor.casefold()
            weight = max(
                1,
                int(_number(row.get("referring_domains"))),
                int(_number(row.get("total_links"))),
            )
            current = profile.setdefault(
                key,
                {"anchor": anchor, "weight": 0, "indexes": set()},
            )
            current["weight"] = max(current["weight"], weight)
            current["indexes"].add(index_name)
    return sorted(profile.values(), key=lambda item: int(item["weight"]), reverse=True)


def _anchor_text_is_brandish(anchor: str, domain: str, title: str) -> bool:
    value = re.sub(r"\s+", " ", str(anchor or "")).strip().casefold()
    if not value:
        return True
    domain_value = str(domain or "").lower().removeprefix("www.")
    domain_root = domain_value.rsplit(".", 1)[0]
    if domain_value and domain_value in value:
        return True
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", f"{domain_root} {title}".lower())
        if len(token) >= 4
    }
    if tokens and any(token in value for token in tokens):
        return True
    return bool(ANCHOR_COMMON_SAFE_RE.match(value))


def anchor_precheck_suspicion_reasons(
    fresh_anchors: Dict[str, Any],
    historic_anchors: Dict[str, Any],
    domain: str,
    title: str,
    max_reasons: int = 5,
) -> List[str]:
    """Cheap router: decide whether a small anchor-only AI check is worth it."""

    items = _anchor_profile_items(fresh_anchors, historic_anchors)
    if not items:
        return []
    locale = resolve_locale(title, domain, "")
    non_cjk_locale = locale not in {"JP", "CN", "KR", "TW", "HK"}
    reasons: List[str] = []
    for item in items:
        anchor = str(item["anchor"])
        weight = int(item["weight"])
        indexes = "+".join(sorted(item["indexes"]))
        short_anchor = anchor[:90]
        if ANCHOR_PRECHECK_SUSPICIOUS_RE.search(anchor):
            reasons.append(f"{indexes}: suspicious anchor pattern, weight {weight}, «{short_anchor}»")
        elif non_cjk_locale and CJK_RE.search(anchor) and not _anchor_text_is_brandish(anchor, domain, title):
            reasons.append(f"{indexes}: unexpected CJK script in anchor, weight {weight}, «{short_anchor}»")
        elif len(anchor) >= 120 and any(ord(ch) > 0xFFFF for ch in anchor):
            reasons.append(f"{indexes}: very long noisy anchor, weight {weight}, «{short_anchor}»")
        if len(reasons) >= max_reasons:
            break
    return list(dict.fromkeys(reasons))


def is_seo_noise_only_reason(reason: Any) -> bool:
    """True when a model reason is only about third-party SEO/PBN backlink runs."""

    text = re.sub(r"\s+", " ", str(reason or "")).strip()
    return bool(
        text
        and (SEO_NOISE_REASON_RE.search(text) or SEO_ANCHOR_NOISE_REASON_RE.search(text))
        and not PROHIBITED_TOPIC_REASON_RE.search(text)
    )


def is_brand_natural_safe_reason(reason: Any) -> bool:
    """Brand/URL/natural-profile wording is not a domain risk by itself."""

    text = re.sub(r"\s+", " ", str(reason or "")).strip()
    if not text or not BRAND_NATURAL_SAFE_REASON_RE.search(text):
        return False
    if PROHIBITED_TOPIC_REASON_RE.search(text):
        return False
    if re.search(r"(?:autogen\w*|автоген\w*)", text, re.IGNORECASE) and not SEO_ANCHOR_NOISE_REASON_RE.search(text):
        return False
    if re.search(r"(?:переиспольз|юзан|reuse|repurpos|смен\w*\s+(?:тем|язык))", text, re.IGNORECASE):
        return False
    return True


def is_unverified_root_redirect_reason(reason: Any) -> bool:
    """AI must not hard-stop on an affiliate/lander root redirect unless forbidden content is explicit."""

    text = re.sub(r"\s+", " ", str(reason or "")).strip()
    return bool(
        text
        and UNVERIFIED_ROOT_REDIRECT_REASON_RE.search(text)
        and not AFFILIATE_REPROFILE_REASON_RE.search(text)
        and not STRONG_FORBIDDEN_TOPIC_REASON_RE.search(text)
    )


def is_independent_critical_reason(reason: Any) -> bool:
    """The screen stage may stop the cascade only for an explicit independent critical signal."""

    text = str(reason or "")
    if NEGATED_RISK_REASON_RE.search(text) and not PROHIBITED_TOPIC_REASON_RE.search(text):
        return False
    if is_unverified_root_redirect_reason(text):
        return False
    return bool(INDEPENDENT_CRITICAL_REASON_RE.search(text))


def sanitize_seo_only_batch(
    value: LinkBatchAssessment,
) -> tuple[LinkBatchAssessment, List[str]]:
    """Prevent a quality batch from turning SEO-only donor noise into domain risk."""

    def ignore(reason: Any) -> bool:
        return (
            is_seo_noise_only_reason(reason)
            or is_brand_natural_safe_reason(reason)
            or is_unverified_root_redirect_reason(reason)
        )

    kept_hard = [reason for reason in value.hard_stop_reasons if not ignore(reason)]
    ignored_hard = [reason for reason in value.hard_stop_reasons if ignore(reason)]
    kept_pbn = [reason for reason in value.pbn_reasons if not ignore(reason)]
    ignored_pbn = [reason for reason in value.pbn_reasons if ignore(reason)]
    pbn_risk = value.pbn_risk
    if pbn_risk != RiskLevel.CLEAN and not kept_pbn and not kept_hard:
        pbn_risk = RiskLevel.CLEAN
    warnings: List[str] = []
    if ignored_hard or ignored_pbn:
        warnings.append("SEO-шум, брендовые/URL/natural и неподтверждённые root-redirect причины исключены из hard stop")
    if value.pbn_risk != RiskLevel.CLEAN and pbn_risk == RiskLevel.CLEAN and not (ignored_hard or ignored_pbn):
        warnings.append("Необоснованный PBN-риск без причины отброшен")
    return (
        value.model_copy(
            update={
                "pbn_risk": pbn_risk,
                "pbn_reasons": kept_pbn,
                "hard_stop_reasons": kept_hard,
            }
        ),
        warnings,
    )


def sanitize_seo_only_anchor(
    value: AnchorScreenAssessment,
) -> tuple[AnchorScreenAssessment, List[str]]:
    """Only the dedicated Fresh/Historic profile may establish anchor risk."""

    def ignore(reason: Any) -> bool:
        return (
            is_seo_noise_only_reason(reason)
            or is_brand_natural_safe_reason(reason)
            or is_unverified_root_redirect_reason(reason)
        )

    kept_reasons = [reason for reason in value.anchor_reasons if not ignore(reason)]
    ignored_reasons = [reason for reason in value.anchor_reasons if ignore(reason)]
    kept_hard = [reason for reason in value.hard_stop_reasons if not ignore(reason)]
    ignored_hard = [reason for reason in value.hard_stop_reasons if ignore(reason)]
    anchor_risk = value.anchor_risk
    if anchor_risk != RiskLevel.CLEAN and not kept_reasons and not kept_hard:
        anchor_risk = RiskLevel.CLEAN
    warnings: List[str] = []
    if ignored_reasons or ignored_hard:
        warnings.append("SEO-анкорный шум, брендовые/URL/natural и неподтверждённые root-redirect причины не использованы как риск всего домена")
    if value.anchor_risk != RiskLevel.CLEAN and anchor_risk == RiskLevel.CLEAN and not (ignored_reasons or ignored_hard):
        warnings.append("Необоснованный анкорный риск без причины отброшен")
    return (
        value.model_copy(
            update={
                "anchor_risk": anchor_risk,
                "anchor_reasons": kept_reasons,
                "hard_stop_reasons": kept_hard,
            }
        ),
        warnings,
    )


def local_backlink_precheck(
    domain: str,
    title: str,
    backlinks: Iterable[Dict[str, Any]],
    strict_mode: bool = False,
    strict_unique_deficit: int = 1,
    strict_article_deficit: int = 1,
) -> Optional[DomainVerdict]:
    """Free upper-bound gate before Anchor pages and any API calls."""

    rows = unique_backlinks(backlinks)
    manual_locale = batch_locale_override(title)
    locale, locale_source = resolve_locale_with_source(title, domain, "")
    unique_margin, _article_margin = near_deficit_limits(
        strict_mode,
        strict_unique_deficit,
        strict_article_deficit,
    )
    if manual_locale:
        required_unique, required_articles = thresholds_for_locale(locale)
        near_unique = max(0, required_unique - unique_margin)
        threshold_note = f"для ручной локали {locale}"
    else:
        required_unique, required_articles = thresholds_for_locale(locale)
        near_unique = min(max(0, thresholds_for_locale(code)[0] - unique_margin) for code in ("LT", "PL", "OTHER"))
        threshold_note = "для самой мягкой возможной локали до AI-уточнения"
    potential = local_backlink_potential(domain, rows)
    if potential["quality_candidates"] >= near_unique:
        if near_unique > 0 and 2 * potential["homepage_candidates"] < near_unique:
            metrics = (
                f"{potential['quality_candidates']}/"
                "AI/"
                f"{potential['homepage_candidates']}"
            )
            return DomainVerdict(
                verdict="REJECT",
                status="BAD:HOMEPAGE_SHARE",
                reason=(
                    f"Локальный жесткий стоп до Anchor/API: на главную ведет максимум "
                    f"{potential['homepage_candidates']} из {potential['quality_candidates']} "
                    f"кандидатов; даже для допуска NEAR нужно минимум {near_unique} доноров "
                    f"{threshold_note}, а при 50% homepage-share доступно не больше "
                    f"{potential['homepage_candidates'] * 2}. Метрика Majestic: {metrics}."
                ),
                locale=locale,
                locale_source=locale_source,
                unique_quality=potential["quality_candidates"],
                article_links=-1,
                homepage_links=potential["homepage_candidates"],
                required_unique=required_unique,
                required_articles=required_articles,
                unique_deficit=max(0, required_unique - potential["quality_candidates"]),
                article_deficit=0,
                hard_stop_reasons=["локально невозможна доля главной 50% при проходном NEAR-профиле"],
                model="LOCAL_RULES",
                early_stop_stage="local_homepage_share_impossible",
            )
        return None
    metrics = (
        f"{potential['quality_candidates']}/"
        "AI/"
        f"{potential['homepage_candidates']}"
    )
    return DomainVerdict(
        verdict="REJECT",
        status="BAD:LOW_PROFILE",
        reason=(
            f"Локальный верхний предел профиля {metrics}; даже для допуска NEAR нужно минимум "
            f"{near_unique} качественных доноров {threshold_note}. "
            "Статейность локально не оценивалась; AI не вызывался."
        ),
        locale=locale,
        locale_source=locale_source,
        unique_quality=potential["quality_candidates"],
        article_links=-1,
        homepage_links=potential["homepage_candidates"],
        required_unique=required_unique,
        required_articles=required_articles,
        unique_deficit=max(0, required_unique - potential["quality_candidates"]),
        article_deficit=0,
        model="LOCAL_RULES",
        early_stop_stage="local_low_profile",
    )


def local_source_age_precheck(
    domain: str,
    title: str,
    backlinks: Iterable[Dict[str, Any]],
    freshness_filter_enabled: bool = True,
    freshness_cutoff_year: int = 2016,
    freshness_max_old_share_percent: int = 50,
) -> Optional[DomainVerdict]:
    """Free freshness gate: obvious old donor URLs/titles before AI."""

    if not freshness_filter_enabled:
        return None

    cutoff_year = clean_freshness_cutoff_year(freshness_cutoff_year)
    max_old_share_percent = clean_freshness_old_share_percent(freshness_max_old_share_percent)
    rows = [row for row in unique_backlinks(backlinks) if local_nonspam_backlink_candidate(row)]
    old_rows = []
    modern_rows = []
    unknown_rows = []
    for row in rows:
        bucket = backlink_age_bucket(row, cutoff_year)
        if bucket == "old":
            old_rows.append(row)
        elif bucket == "modern":
            modern_rows.append(row)
        else:
            unknown_rows.append(row)

    if len(old_rows) < 2:
        return None

    old_share = len(old_rows) / max(1, len(rows))
    no_modern_signal = not modern_rows
    too_many_old = old_share > (max_old_share_percent / 100)
    if not no_modern_signal and not too_many_old:
        return None

    locale, locale_source = resolve_locale_with_source(title, domain, "")
    required_unique, required_articles = thresholds_for_locale(locale)
    examples = "; ".join(
        re.sub(
            r"\s+",
            " ",
            str(row.get("source_title") or row.get("source_url") or ""),
        )[:120]
        for row in old_rows[:3]
    )
    if no_modern_signal:
        age_problem = (
            f"нет ни одной не-спамной ссылки с подтверждённым годом {cutoff_year}+ "
            f"(старых {len(old_rows)}, возраст неизвестен у {len(unknown_rows)})"
        )
    else:
        age_problem = (
            f"старые ссылки до {cutoff_year}: {len(old_rows)} из {len(rows)} "
            f"({old_share:.0%}), выше лимита {max_old_share_percent}%"
        )
    reason = f"Локальный стоп по свежести: {age_problem}. Примеры: {examples}. AI не вызывался."
    return DomainVerdict(
        verdict="REJECT",
        status="BAD:STALE_PROFILE",
        reason=reason,
        locale=locale,
        locale_source=locale_source,
        old_links=len(old_rows),
        modern_links=len(modern_rows),
        unknown_age_links=len(unknown_rows),
        required_unique=required_unique,
        required_articles=required_articles,
        hard_stop_reasons=[reason],
        model="LOCAL_RULES",
        early_stop_stage="local_source_age",
    )


LOCAL_LINK_SPAM_RE = re.compile(
    r"(?:aged domains?|expired domains?|buy backlinks?|seo backlinks?|pbn (?:service|network)|"
    r"rankvance|domains\.com\.bz|top domains?|domain lists?|link[ -]?building|website[- ]?list|/all/\d+|backlink agency|"
    r"premium backlinks?|high quality dofollow backlinks?)",
    re.IGNORECASE,
)
LOCAL_LISTING_RE = re.compile(
    r"(?:directory|verzeichnis|branchenbuch|website[- ]?list|link[- ]?list|"
    r"/tags?/|/category/|/author/|/search/|/profile/|/all/\d+)",
    re.IGNORECASE,
)


def local_nonspam_backlink_candidate(row: Dict[str, Any]) -> bool:
    """Unique-count rule: count weak directories/references, exclude only clear spam."""

    source_url = str(row.get("source_url") or "").strip()
    if not source_url:
        return False
    combined = " ".join(
        (
            source_url,
            re.sub(r"\s+", " ", str(row.get("source_title") or "")),
            str(row.get("source_topic") or ""),
            str(row.get("anchor") or ""),
        )
    )
    if LOCAL_LINK_SPAM_RE.search(combined):
        return False
    outbound = _number(row.get("outbound_external"))
    ext_domains = _number(row.get("external_domains"))
    return outbound < 1000 and ext_domains < 800


def local_backlink_potential(
    domain: str,
    rows: Iterable[Dict[str, Any]],
) -> Dict[str, int]:
    """Conservative upper bound used only to decide whether the quality model is worth calling."""

    unique_rows = unique_backlinks(rows)
    quality_candidates = 0
    homepage_candidates = 0
    for row in unique_rows:
        if not local_nonspam_backlink_candidate(row):
            continue
        quality_candidates += 1
        if is_exact_homepage(str(row.get("target_url") or ""), domain):
            homepage_candidates += 1
    return {
        "raw_unique": len(unique_rows),
        "quality_candidates": quality_candidates,
        "homepage_candidates": homepage_candidates,
    }


def _page_url_parts(url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return "", ""
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return host, path


def _is_exact_page_root(url: str, domain: str) -> bool:
    host, path = _page_url_parts(url)
    expected = str(domain or "").lower().removeprefix("www.")
    return host == expected and path == "/"


def _historic_page_text(row: Dict[str, Any]) -> str:
    return " ".join(
        str(row.get(name) or "")
        for name in (
            "page_title",
            "page_url",
            "crawl_result",
            "language",
            "redirect_url",
            "last_seen",
        )
    )


def _years_from_historic_page(row: Dict[str, Any]) -> List[int]:
    text = _historic_page_text(row)
    years: List[int] = []
    for value in re.findall(r"\b(?:19|20)\d{2}\b", text):
        try:
            years.append(int(value))
        except ValueError:
            continue
    return years


def _is_meaningful_old_inner_page(row: Dict[str, Any], domain: str) -> bool:
    url = str(row.get("page_url") or "")
    if _is_exact_page_root(url, domain):
        return False
    _, path = _page_url_parts(url)
    if not path or path == "/" or HISTORIC_PAGE_IGNORE_PATH_RE.search(path):
        return False
    years = _years_from_historic_page(row)
    explicit_old = any(year <= 2019 for year in years)
    meaningful_path = bool(HISTORIC_PAGE_MEANINGFUL_PATH_RE.search(path))
    title = str(row.get("page_title") or "")
    titled_old_page = bool(title and WESTERN_LETTER_RE.search(title) and explicit_old)
    return explicit_old or meaningful_path or titled_old_page


def _short_page_example(row: Dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(row.get("page_title") or "")).strip()
    url = re.sub(r"\s+", " ", str(row.get("page_url") or "")).strip()
    last_seen = re.sub(r"\s+", " ", str(row.get("last_seen") or "")).strip()
    value = title or url
    if title and url:
        value = f"{title} ({url})"
    if last_seen:
        value = f"{value}, Last Seen {last_seen}"
    return value[:180]


def _dedupe_historic_page_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("page_url") or "")
        title = re.sub(r"\s+", " ", str(row.get("page_title") or "")).strip().casefold()
        host, path = _page_url_parts(url)
        key = f"{host}{path}".casefold() if host or path else title
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _historic_forbidden_inner_rows(rows: Iterable[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for row in rows:
        url = str(row.get("page_url") or "")
        if _is_exact_page_root(url, domain):
            continue
        _, path = _page_url_parts(url)
        if not path or path == "/" or HISTORIC_PAGE_IGNORE_PATH_RE.search(path):
            continue
        if HISTORIC_PAGE_FORBIDDEN_RE.search(_historic_page_text(row)):
            result.append(row)
    return _dedupe_historic_page_rows(result)


def _historic_wp_asset_doorway_rows(rows: Iterable[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    expected = str(domain or "").lower().removeprefix("www.")
    for row in rows:
        host, path = _page_url_parts(str(row.get("page_url") or ""))
        if host and host != expected:
            continue
        if not path or not HISTORIC_PAGE_WP_ASSET_HTML_RE.search(path):
            continue
        text = _historic_page_text(row)
        random_asset_html = bool(HISTORIC_PAGE_WP_ASSET_RANDOM_RE.search(path))
        product_doorway_text = bool(HISTORIC_PAGE_PRODUCT_DOORWAY_RE.search(text))
        if random_asset_html or product_doorway_text:
            result.append(row)
    return _dedupe_historic_page_rows(result)


def local_historic_pages_precheck(
    domain: str,
    title: str,
    pages_report: Dict[str, Any],
) -> Optional[DomainVerdict]:
    """Detect obvious post-drop reuse from Majestic Historic Pages before AI."""

    rows = list(pages_report.get("rows", []))
    locale, locale_source = resolve_locale_with_source(title, domain, "")
    required_unique, required_articles = thresholds_for_locale(locale)
    forbidden_inner_rows = _historic_forbidden_inner_rows(rows, domain)
    strong_forbidden_rows = [
        row
        for row in forbidden_inner_rows
        if HISTORIC_PAGE_STRONG_FORBIDDEN_RE.search(_historic_page_text(row))
    ]
    if strong_forbidden_rows:
        examples = "; ".join(_short_page_example(row) for row in strong_forbidden_rows[:3])
        reason = (
            "Historic Pages показывает сильный запрещённый след в истории проверяемого домена: "
            f"{examples}. Это признак переиспользования под crypto/krypto signals/trading. "
            "AI не вызывался."
        )
        return DomainVerdict(
            verdict="REJECT",
            status="BAD:HISTORIC_PAGES",
            reason=reason,
            locale=locale,
            locale_source=locale_source,
            required_unique=required_unique,
            required_articles=required_articles,
            model="LOCAL_RULES",
            early_stop_stage="local_historic_pages",
            hard_stop_reasons=[reason],
        )
    if len(forbidden_inner_rows) >= 2:
        examples = "; ".join(_short_page_example(row) for row in forbidden_inner_rows[:3])
        reason = (
            "Historic Pages показывает запрещённые внутренние страницы проверяемого домена: "
            f"{examples}. Это признак переиспользования под casino/adult/pharma/другую запрещённую тематику. "
            "AI не вызывался."
        )
        return DomainVerdict(
            verdict="REJECT",
            status="BAD:HISTORIC_PAGES",
            reason=reason,
            locale=locale,
            locale_source=locale_source,
            required_unique=required_unique,
            required_articles=required_articles,
            model="LOCAL_RULES",
            early_stop_stage="local_historic_pages",
            hard_stop_reasons=[reason],
        )

    wp_asset_rows = _historic_wp_asset_doorway_rows(rows, domain)
    if len(wp_asset_rows) >= 3:
        examples = "; ".join(_short_page_example(row) for row in wp_asset_rows[:3])
        reason = (
            "Historic Pages показывает пачку индексируемых HTML-страниц в технических WordPress-директориях "
            f"(/wp-content или /wp-includes): {examples}. Похоже на взломанный WordPress, товарный дорвей или автоген. "
            "AI не вызывался."
        )
        return DomainVerdict(
            verdict="REJECT",
            status="BAD:HISTORIC_PAGES",
            reason=reason,
            locale=locale,
            locale_source=locale_source,
            required_unique=required_unique,
            required_articles=required_articles,
            model="LOCAL_RULES",
            early_stop_stage="local_historic_pages",
            hard_stop_reasons=[reason],
        )

    if len(rows) < 4:
        return None
    root_rows = [row for row in rows if _is_exact_page_root(str(row.get("page_url") or ""), domain)]
    old_inner_rows = [row for row in rows if _is_meaningful_old_inner_page(row, domain)]
    if len(old_inner_rows) < 2 or not root_rows:
        return None

    non_cjk_locale = locale not in {"JP", "CN", "KR", "TW", "HK"}
    root_problem_rows: List[Dict[str, Any]] = []
    for row in root_rows:
        text = _historic_page_text(row)
        forbidden = bool(HISTORIC_PAGE_FORBIDDEN_RE.search(text))
        cjk_language_shift = non_cjk_locale and bool(CJK_RE.search(text))
        if forbidden or cjk_language_shift:
            root_problem_rows.append(row)

    if not root_problem_rows:
        return None

    root_example = _short_page_example(root_problem_rows[0])
    old_examples = "; ".join(_short_page_example(row) for row in old_inner_rows[:3])
    reason = (
        "Historic Pages показывает признаки переиспользования: проблемная поздняя главная "
        f"«{root_example}» и старые внутренние страницы прежнего сайта: {old_examples}. "
        "AI не вызывался."
    )
    return DomainVerdict(
        verdict="REJECT",
        status="BAD:HISTORIC_PAGES",
        reason=reason,
        locale=locale,
        locale_source=locale_source,
        required_unique=required_unique,
        required_articles=required_articles,
        model="LOCAL_RULES",
        early_stop_stage="local_historic_pages",
        hard_stop_reasons=[reason],
    )


def compact_historic_pages_payload(
    pages_report: Dict[str, Any],
    max_rows: int = 15,
) -> tuple[List[str], List[List[Any]], bool]:
    """Small Pages/Historic context for the quality model; rows are not backlinks."""

    rows = list(pages_report.get("rows", []))

    def score(row: Dict[str, Any]) -> tuple[int, float, float]:
        text = _historic_page_text(row)
        forbidden = int(bool(HISTORIC_PAGE_FORBIDDEN_RE.search(text)))
        dated = int(bool(_years_from_historic_page(row)))
        weight = max(_number(row.get("referring_urls")), _number(row.get("referring_domains")))
        return (forbidden, dated, weight)

    selected = sorted(rows, key=score, reverse=True)[:max_rows]
    columns = [
        "url",
        "title",
        "crawl",
        "lang",
        "redirect",
        "ref_urls",
        "in_links",
        "ref_domains",
        "last_seen",
    ]
    compact_rows = [
        [
            str(row.get("page_url") or "")[:420],
            str(row.get("page_title") or "")[:220],
            str(row.get("crawl_result") or "")[:80],
            str(row.get("language") or "")[:100],
            str(row.get("redirect_url") or "")[:320],
            row.get("referring_urls"),
            row.get("inbound_links"),
            row.get("referring_domains"),
            str(row.get("last_seen") or "")[:40],
        ]
        for row in selected
    ]
    truncated = bool(pages_report.get("truncated")) or len(rows) > len(selected)
    return columns, compact_rows, truncated


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_metric_number(value: Any) -> str:
    number = _number(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _ceil_metric_number(value: Any) -> float:
    number = _number(value)
    if number <= 0:
        return 0.0
    integer = int(number)
    return float(integer if number == integer else integer + 1)


def _is_public_http_url(url: str) -> bool:
    """Reject local/private destinations before fetching untrusted donor URLs."""

    try:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            return False
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
            if not ip.is_global:
                return False
        return True
    except (OSError, ValueError):
        return False


class _PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_public_http_url(newurl):
            raise URLError("redirect to a non-public URL was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _clean_visible_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_host(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("www.")


def _url_host(value: Any) -> str:
    try:
        return _normalize_host(urlparse(str(value or "")).hostname or "")
    except ValueError:
        return ""


def _target_hosts_from_url(value: Any) -> set[str]:
    host = _url_host(value)
    return {host} if host else set()


def _host_matches_target(host: str, target_hosts: set[str]) -> bool:
    clean = _normalize_host(host)
    return any(clean == target or clean.endswith(f".{target}") for target in target_hosts if target)


def _link_dom_area(stack: Sequence[str]) -> str:
    tags = set(stack)
    parts: List[str] = []
    if "article" in tags or "main" in tags:
        parts.append("content")
    if "p" in tags:
        parts.append("paragraph")
    if "li" in tags or "ul" in tags or "ol" in tags:
        parts.append("list")
    if "table" in tags or "td" in tags or "tr" in tags:
        parts.append("table")
    if "aside" in tags:
        parts.append("sidebar")
    if "nav" in tags:
        parts.append("nav")
    if "footer" in tags:
        parts.append("footer")
    if "header" in tags:
        parts.append("header")
    return "+".join(parts) if parts else "body"


class _VisiblePageParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, source_url: str = "", target_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = str(source_url or "")
        self.source_host = _url_host(source_url)
        self.target_hosts = _target_hosts_from_url(target_url)
        self.skip_depth = 0
        self.content_depth = 0
        self.in_title = False
        self.tag_stack: List[str] = []
        self.anchor_stack: List[Dict[str, Any] | None] = []
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []
        self.content_parts: List[str] = []
        self.description = ""
        self.visible_len = 0
        self.total_link_count = 0
        self.external_link_count = 0
        self.target_links: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        values = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag not in self.VOID_TAGS:
            self.tag_stack.append(tag)
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        if tag in {"article", "main"}:
            self.content_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            if key in {"description", "og:description", "twitter:description"} and not self.description:
                self.description = values.get("content", "")
        if tag == "a":
            href = values.get("href", "")
            absolute_href = urljoin(self.source_url, href) if href else ""
            host = _url_host(absolute_href)
            if host:
                self.total_link_count += 1
                if self.source_host and host != self.source_host:
                    self.external_link_count += 1
                elif not self.source_host:
                    self.external_link_count += 1
            if host and _host_matches_target(host, self.target_hosts):
                link = {
                    "href": absolute_href[:500],
                    "text_parts": [],
                    "start": self.visible_len,
                    "end": self.visible_len,
                    "area": _link_dom_area(self.tag_stack),
                }
                self.target_links.append(link)
                self.anchor_stack.append(link)
            else:
                self.anchor_stack.append(None)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.anchor_stack:
            self.anchor_stack.pop()
        if tag in {"article", "main"} and self.content_depth:
            self.content_depth -= 1
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        for index in range(len(self.tag_stack) - 1, -1, -1):
            if self.tag_stack[index] == tag:
                del self.tag_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        value = _clean_visible_text(data)
        if not value:
            return
        if self.in_title:
            self.title_parts.append(value)
        if not self.skip_depth:
            start = self.visible_len + (1 if self.text_parts else 0)
            self.text_parts.append(value)
            self.visible_len = start + len(value)
            if self.content_depth:
                self.content_parts.append(value)
            if self.anchor_stack and self.anchor_stack[-1] is not None:
                link = self.anchor_stack[-1]
                link["text_parts"].append(value)
                if not link.get("start"):
                    link["start"] = start
                link["end"] = self.visible_len

    def page_link_evidence(self, visible_text: str, context_chars: int = 700) -> Dict[str, Any]:
        contexts: List[str] = []
        texts: List[str] = []
        areas: List[str] = []
        half = max(80, context_chars // 2)
        for link in self.target_links[:3]:
            text = _clean_visible_text(" ".join(link.get("text_parts") or []))
            if text:
                texts.append(text[:160])
            area = str(link.get("area") or "body")
            if area:
                areas.append(area)
            start = int(link.get("start") or 0)
            end = max(start, int(link.get("end") or start))
            left = max(0, start - half)
            right = min(len(visible_text), end + half)
            context = _clean_visible_text(visible_text[left:right])
            if context:
                contexts.append(context[:context_chars])
        visible_chars = len(visible_text)
        density = round(self.external_link_count / max(1.0, visible_chars / 1000), 1)
        return {
            "target_link_found": bool(self.target_links),
            "target_link_count": len(self.target_links),
            "target_link_texts": "; ".join(dict.fromkeys(texts))[:300],
            "link_dom_area": "; ".join(dict.fromkeys(areas))[:160],
            "link_context_excerpt": " | ".join(dict.fromkeys(contexts))[:context_chars],
            "external_links_count": self.external_link_count,
            "total_links_count": self.total_link_count,
            "visible_text_chars": visible_chars,
            "external_link_density": density,
        }


def extract_article_page_preview(
    html: str,
    source_url: str = "",
    target_url: str = "",
    max_chars: int = 1200,
) -> Dict[str, Any]:
    parser = _VisiblePageParser(source_url=source_url, target_url=target_url)
    parser.feed(html or "")
    title = _clean_visible_text(" ".join(parser.title_parts))[:300]
    description = _clean_visible_text(parser.description)[:500]
    all_visible_text = _clean_visible_text(" ".join(parser.text_parts))
    preferred_parts = parser.content_parts if len(" ".join(parser.content_parts)) >= 240 else parser.text_parts
    visible_text = _clean_visible_text(" ".join(preferred_parts))
    preview = {
        "page_title": title,
        "description": description,
        "text_excerpt": visible_text[:max_chars],
    }
    preview.update(parser.page_link_evidence(all_visible_text))
    return preview


@lru_cache(maxsize=512)
def fetch_article_page(url: str, max_chars: int = 1200, target_url: str = "") -> Dict[str, Any]:
    """Fetch a bounded HTML preview that the quality model can inspect for article verification."""

    source_url = str(url or "").strip()
    result: Dict[str, Any] = {
        "source_url": source_url,
        "status": "ERROR",
        "http_status": 0,
        "final_url": "",
        "page_title": "",
        "description": "",
        "text_excerpt": "",
        "target_link_found": False,
        "target_link_count": 0,
        "target_link_texts": "",
        "link_dom_area": "",
        "link_context_excerpt": "",
        "external_links_count": 0,
        "total_links_count": 0,
        "visible_text_chars": 0,
        "external_link_density": 0.0,
        "error": "",
    }
    if not _is_public_http_url(source_url):
        result["status"] = "BLOCKED_URL"
        result["error"] = "URL is not a public HTTP(S) destination"
        return result

    request = Request(
        source_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "de,en;q=0.8,*;q=0.5",
        },
    )
    try:
        opener = build_opener(_PublicRedirectHandler())
        with opener.open(request, timeout=10) as response:
            result["http_status"] = int(getattr(response, "status", 200) or 200)
            result["final_url"] = str(response.geturl() or source_url)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if content_type and not any(value in content_type for value in ("text/html", "application/xhtml+xml")):
                result["status"] = "UNSUPPORTED_CONTENT"
                result["error"] = content_type[:160]
                return result
            body = response.read(262_145)[:262_144]
            charset = response.headers.get_content_charset() or "utf-8"
        result.update(
            extract_article_page_preview(
                body.decode(charset, errors="replace"),
                source_url=source_url,
                target_url=target_url,
                max_chars=max_chars,
            )
        )
        result["status"] = "OK"
    except HTTPError as exc:
        result["status"] = "HTTP_ERROR"
        result["http_status"] = int(getattr(exc, "code", 0) or 0)
        result["error"] = str(exc)[:240]
    except (URLError, OSError, ValueError) as exc:
        result["status"] = "FETCH_ERROR"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return result


def _format_article_page_evidence(
    rows: Sequence[Dict[str, Any]],
    fetched: Dict[str, Dict[str, Any]],
    max_chars: int,
) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for row in rows:
        record_id = str(row.get("record_id") or "")
        page = fetched.get(record_id, {})
        evidence.append(
            {
                "id": record_id,
                "source_url": str(row.get("source_url") or "")[:500],
                "majestic_title": str(row.get("source_title") or "")[:320],
                "anchor": str(row.get("anchor") or "")[:240],
                "fetch_status": str(page.get("status") or "FETCH_ERROR"),
                "http_status": page.get("http_status", 0),
                "final_url": str(page.get("final_url") or "")[:500],
                "page_title": str(page.get("page_title") or "")[:300],
                "description": str(page.get("description") or "")[:500],
                "text_excerpt": str(page.get("text_excerpt") or "")[:max_chars],
                "target_link_found": bool(page.get("target_link_found", False)),
                "target_link_count": int(page.get("target_link_count") or 0),
                "target_link_texts": str(page.get("target_link_texts") or "")[:300],
                "link_dom_area": str(page.get("link_dom_area") or "")[:160],
                "link_context_excerpt": str(page.get("link_context_excerpt") or "")[:700],
                "external_links_count": int(page.get("external_links_count") or 0),
                "visible_text_chars": int(page.get("visible_text_chars") or 0),
                "external_link_density": page.get("external_link_density") or 0,
                "fetch_error": str(page.get("error") or "")[:240],
            }
        )
    return evidence


def collect_article_page_evidence(
    rows: Sequence[Dict[str, Any]],
    candidate_ids: Iterable[str],
    max_pages: int = 12,
    max_chars: int = 1200,
) -> tuple[List[Dict[str, Any]], int]:
    """Fetch only AI-selected article candidates, in parallel and with a hard cap."""

    wanted = set(map(str, candidate_ids))
    selected = [row for row in rows if str(row.get("record_id")) in wanted][:max_pages]
    fetched: Dict[str, Dict[str, Any]] = {}
    if selected:
        with ThreadPoolExecutor(max_workers=min(6, len(selected))) as pool:
            futures = {
                pool.submit(
                    fetch_article_page,
                    str(row.get("source_url") or ""),
                    max_chars,
                    str(row.get("target_url") or ""),
                ): str(row.get("record_id"))
                for row in selected
            }
            for future in as_completed(futures):
                record_id = futures[future]
                try:
                    fetched[record_id] = future.result()
                except Exception as exc:  # pragma: no cover - worker isolation
                    fetched[record_id] = {
                        "status": "FETCH_ERROR",
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }

    return _format_article_page_evidence(selected, fetched, max_chars), max(0, len(wanted) - len(selected))


def collect_browser_article_page_evidence(
    rows: Sequence[Dict[str, Any]],
    candidate_ids: Iterable[str],
    page_fetcher: Callable[..., Dict[str, Any]],
    max_pages: int = 4,
    max_chars: int = 1200,
) -> tuple[List[Dict[str, Any]], int]:
    """Use the visible browser only for borderline pages HTTP could not verify."""

    wanted = set(map(str, candidate_ids))
    selected = [row for row in rows if str(row.get("record_id")) in wanted][:max_pages]
    fetched: Dict[str, Dict[str, Any]] = {}
    for row in selected:
        record_id = str(row.get("record_id") or "")
        source_url = str(row.get("source_url") or "")
        if not _is_public_http_url(source_url):
            fetched[record_id] = {
                "status": "BLOCKED_URL",
                "http_status": 0,
                "final_url": "",
                "page_title": "",
                "description": "",
                "text_excerpt": "",
                "error": "URL is not a public HTTP(S) destination",
            }
            continue
        try:
            try:
                fetched[record_id] = page_fetcher(source_url, max_chars, str(row.get("target_url") or ""))
            except TypeError:
                fetched[record_id] = page_fetcher(source_url, max_chars)
        except Exception as exc:
            fetched[record_id] = {
                "status": "BROWSER_FETCH_ERROR",
                "http_status": 0,
                "final_url": "",
                "page_title": "",
                "description": "",
                "text_excerpt": "",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
    return _format_article_page_evidence(selected, fetched, max_chars), max(0, len(wanted) - len(selected))


def sort_backlinks_for_ai(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Put likely editorial links first so good profiles reach their threshold sooner."""

    def score(row: Dict[str, Any]) -> tuple[float, float, float]:
        trust = max(_number(row.get("source_domain_tf")), _number(row.get("source_url_tf")))
        external = _number(row.get("outbound_external"))
        text_signal = float(bool(row.get("source_title"))) + float(bool(row.get("source_topic")))
        return (text_signal, trust, -external)

    return sorted(unique_backlinks(rows), key=score, reverse=True)


def sort_backlinks_for_critical(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Put independent prohibited/reuse signals first for the small screen batch."""

    def score(row: Dict[str, Any]) -> tuple[float, float, float]:
        combined = " ".join(
            str(row.get(name) or "")
            for name in (
                "source_domain",
                "source_url",
                "source_title",
                "source_topic",
                "target_title",
                "target_topic",
            )
        )
        independent_risk = float(bool(INDEPENDENT_CRITICAL_REASON_RE.search(combined)))
        external = max(_number(row.get("outbound_external")), _number(row.get("external_domains")))
        trust = max(_number(row.get("source_domain_tf")), _number(row.get("source_url_tf")))
        return (independent_risk, external, trust)

    return sorted(unique_backlinks(rows), key=score, reverse=True)


def compact_anchor_payload(
    evidence: Dict[str, Any],
    max_per_index: int,
) -> tuple[Dict[str, Any], int]:
    """Use column arrays and deduplicate texts to avoid repeated JSON keys."""

    def rows_for(name: str) -> List[List[Any]]:
        result: List[List[Any]] = []
        seen: set[str] = set()
        for row in evidence.get(name, []):
            anchor = re.sub(r"\s+", " ", str(row.get("anchor") or "")).strip()
            key = anchor.casefold()
            if not anchor or key in seen:
                continue
            seen.add(key)
            result.append(
                [
                    anchor[:240],
                    str(row.get("topic") or "")[:120],
                    row.get("referring_domains"),
                    row.get("total_links"),
                ]
            )
            if len(result) >= max_per_index:
                break
        return result

    fresh = rows_for("anchors_fresh")
    historic = rows_for("anchors_historic")
    payload = {
        "domain": evidence["domain"],
        "locale_hint": evidence["batch_locale_hint"],
        "columns": ["anchor", "topic", "ref_domains", "total_links"],
        "fresh": fresh,
        "historic": historic,
    }
    return payload, len(fresh) + len(historic)


def compact_backlink_batch(
    domain: str,
    locale_hint: str,
    rows: Sequence[Dict[str, Any]],
    batch_number: int,
    total_batches: int,
) -> Dict[str, Any]:
    """Encode rows as arrays; repeated object keys were a large share of input tokens."""

    return {
        "domain": domain,
        "locale_hint": locale_hint,
        "batch": batch_number,
        "total_batches": total_batches,
        "columns": [
            "id",
            "donor",
            "source_url",
            "source_title",
            "source_topic",
            "language",
            "anchor",
            "url_tf",
            "url_cf",
            "domain_tf",
            "domain_cf",
            "out_external",
            "external_domains",
            "target_title",
            "target_topic",
        ],
        "rows": [
            [
                row.get("record_id"),
                str(row.get("source_domain") or "")[:180],
                str(row.get("source_url") or "")[:500],
                str(row.get("source_title") or "")[:320],
                str(row.get("source_topic") or "")[:160],
                str(row.get("language") or "")[:40],
                str(row.get("anchor") or "")[:240],
                row.get("source_url_tf"),
                row.get("source_url_cf"),
                row.get("source_domain_tf"),
                row.get("source_domain_cf"),
                row.get("outbound_external"),
                row.get("external_domains"),
                str(row.get("target_title") or "")[:240],
                str(row.get("target_topic") or "")[:120],
            ]
            for row in rows
        ],
    }


def compact_critical_payload(
    domain: str,
    locale_hint: str,
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Send only fields useful for finding confident critical stop signals."""

    return {
        "domain": domain,
        "locale_hint": locale_hint,
        "columns": [
            "donor",
            "source_url",
            "source_title",
            "source_topic",
            "language",
            "out_external",
            "external_domains",
            "target_title",
            "target_topic",
        ],
        "rows": [
            [
                str(row.get("source_domain") or "")[:180],
                str(row.get("source_url") or "")[:500],
                str(row.get("source_title") or "")[:320],
                str(row.get("source_topic") or "")[:160],
                str(row.get("language") or "")[:40],
                row.get("outbound_external"),
                row.get("external_domains"),
                str(row.get("target_title") or "")[:240],
                str(row.get("target_topic") or "")[:120],
            ]
            for row in rows
        ],
    }


def aggregate_assessment(
    domain: str,
    title: str,
    backlinks: Iterable[Dict[str, Any]],
    assessment: DomainEvidenceAssessment | Dict[str, Any],
    model: str = "",
    strict_mode: bool = False,
    strict_unique_deficit: int = 1,
    strict_article_deficit: int = 1,
    freshness_filter_enabled: bool = True,
    freshness_cutoff_year: int = 2016,
    freshness_max_old_share_percent: int = 50,
) -> DomainVerdict:
    rows = list(backlinks)
    rows_by_id = {str(row.get("record_id")): row for row in rows if row.get("record_id")}
    locale, locale_source = resolve_locale_with_source(title, domain, _value(assessment, "locale", ""))
    if locale == "OTHER" and locale_source == "FALLBACK":
        inferred_locale, inferred_source = infer_locale_from_backlinks_with_source(rows)
        if inferred_locale:
            locale, locale_source = inferred_locale, inferred_source
    required_unique, required_articles = thresholds_for_locale(locale)
    cutoff_year = clean_freshness_cutoff_year(freshness_cutoff_year)
    max_old_share_percent = clean_freshness_old_share_percent(freshness_max_old_share_percent)

    hard_stops = [str(x).strip() for x in _value(assessment, "hard_stop_reasons", []) if str(x).strip()]
    warnings = [str(x).strip() for x in _value(assessment, "warnings", []) if str(x).strip()]
    pbn_risk = _enum_value(_value(assessment, "pbn_risk", "CLEAN"))
    anchor_risk = _enum_value(_value(assessment, "anchor_risk", "CLEAN"))
    if pbn_risk == RiskLevel.SPAM.value:
        hard_stops.append("Явные признаки PBN/юзаного заспамленного дропа")
    if anchor_risk == RiskLevel.SPAM.value:
        hard_stops.append("Исторические анкоры указывают на спам")

    unique_quality = 0
    article_links = 0.0
    half_article_units = 0.0
    homepage_links = 0
    old_links = 0
    modern_links = 0
    unknown_age_links = 0
    link_year_values: List[int] = []
    seen_keys: set[str] = set()
    assessed_ids: set[str] = set()

    for item in _value(assessment, "link_assessments", []):
        record_id = str(_value(item, "record_id", ""))
        row = rows_by_id.get(record_id)
        if row is None or record_id in assessed_ids:
            continue
        assessed_ids.add(record_id)
        prohibited = _enum_value(_value(item, "prohibited_topic", "NONE"))
        quality = _enum_value(_value(item, "quality", "UNCERTAIN"))
        count_quality = bool(_value(item, "count_quality", False))
        if quality != LinkQuality.QUALITY.value or not count_quality or prohibited != ProhibitedTopic.NONE.value:
            continue
        key = _canonical_source_key(row)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_quality += 1
        if (
            bool(_value(item, "count_article", False))
            and _enum_value(_value(item, "link_type", "OTHER")) == LinkType.ARTICLE.value
        ):
            article_weight = max(0.0, min(1.0, _number(_value(item, "article_weight", 1.0))))
            if article_weight >= 1.0:
                article_links += 1.0
            elif article_weight > 0:
                half_article_units += article_weight
        if is_exact_homepage(str(row.get("target_url") or ""), domain):
            homepage_links += 1
        source_years = backlink_source_years(row)
        if source_years:
            link_year_values.append(max(source_years))
        age_signal = _enum_value(_value(item, "age_signal", "UNKNOWN"))
        age_bucket = "old" if source_years and max(source_years) < cutoff_year else "modern" if source_years else "unknown"
        if age_bucket == "old":
            old_links += 1
        elif age_bucket == "modern":
            modern_links += 1
        elif age_signal == AgeSignal.OLD.value:
            old_links += 1
        else:
            unknown_age_links += 1

    article_links = _ceil_metric_number(article_links + min(2.0, half_article_units))
    missing_assessments = max(0, len(rows_by_id) - len(assessed_ids))
    if missing_assessments:
        warnings.append(f"Модель не классифицировала ссылок: {missing_assessments}; они не засчитаны")

    hard_stops = list(dict.fromkeys(hard_stops))
    unique_deficit = max(0, required_unique - unique_quality)
    article_deficit = max(0, required_articles - article_links)
    article_deficit_label = _format_metric_number(article_deficit)
    unique_margin, article_margin = near_deficit_limits(
        strict_mode,
        strict_unique_deficit,
        strict_article_deficit,
    )
    near_unique, near_articles = near_thresholds_for_locale(
        locale,
        strict_mode,
        strict_unique_deficit,
        strict_article_deficit,
    )
    article_links_label = _format_metric_number(article_links)
    metrics = f"{unique_quality}/{article_links_label}/{homepage_links}"
    soft_profile_risk = pbn_risk == RiskLevel.RISK.value or anchor_risk == RiskLevel.RISK.value
    non_blocking_negatives: List[str] = []
    homepage_hard_stop = ""
    stale_profile_stop = ""
    if unique_quality:
        homepage_share = homepage_links / unique_quality
        old_share = old_links / unique_quality
        if homepage_share < 0.50:
            homepage_hard_stop = (
                f"на главную ведет только {homepage_links} из {unique_quality} "
                f"({homepage_share:.0%}), ниже обязательных 50%"
            )
        if freshness_filter_enabled:
            if old_links >= 2 and modern_links == 0:
                stale_profile_stop = (
                    f"есть {old_links} качественных ссылок до {cutoff_year}, "
                    f"но нет ни одной подтвержденной качественной ссылки {cutoff_year} года или новее"
                )
            elif old_share > (max_old_share_percent / 100):
                stale_profile_stop = (
                    f"ссылки до {cutoff_year}: {old_links} из {unique_quality} "
                    f"({old_share:.0%}), выше лимита {max_old_share_percent}%"
                )
            elif old_links:
                non_blocking_negatives.append(
                    f"ссылки до {cutoff_year}: {old_links} из {unique_quality} ({old_share:.0%}), лимит {max_old_share_percent}%"
                )
    if unknown_age_links:
        warnings.append(f"Ссылки с неизвестным возрастом: {unknown_age_links} из {unique_quality}")
    warnings.extend(non_blocking_negatives)

    if hard_stops:
        verdict = "REJECT"
        status = "BAD:AI_HARD_STOP"
        reason = f"Жесткий стоп: {'; '.join(hard_stops)}. Метрика Majestic: {metrics}."
    elif stale_profile_stop:
        verdict = "REJECT"
        status = "BAD:STALE_PROFILE"
        reason = f"Возраст ссылочного не проходит: {stale_profile_stop}. Метрика Majestic: {metrics}."
    elif homepage_hard_stop:
        verdict = "REJECT"
        status = "BAD:HOMEPAGE_SHARE"
        reason = f"Жесткий стоп: {homepage_hard_stop}. Метрика Majestic: {metrics}."
    elif soft_profile_risk:
        verdict = "REJECT"
        status = "BAD:AI_RISK"
        risk_parts = []
        if pbn_risk == RiskLevel.RISK.value:
            risk_parts.extend(_value(assessment, "pbn_reasons", []) or ["риск PBN/юзаного дропа"])
        if anchor_risk == RiskLevel.RISK.value:
            risk_parts.extend(_value(assessment, "anchor_reasons", []) or ["риск в анкорах"])
        reason = f"Негативные сигналы: {'; '.join(map(str, risk_parts))}. Метрика Majestic: {metrics}."
    elif unique_deficit == 0 and article_deficit == 0:
        verdict = "PASS"
        status = "GOOD"
        note = f" Негативные, но не стоп-сигналы: {'; '.join(non_blocking_negatives)}." if non_blocking_negatives else ""
        reason = f"Порог для {locale} выполнен. Метрика Majestic: {metrics}.{note}"
    elif unique_deficit <= unique_margin and article_deficit <= article_margin:
        verdict = "PASS_NEAR_THRESHOLD"
        status = "GOOD (NEAR THRESHOLD)"
        strict_note = " Strict mode включен." if strict_mode else ""
        note = f" Негативные, но не стоп-сигналы: {'; '.join(non_blocking_negatives)}." if non_blocking_negatives else ""
        reason = (
            f"Домен хороший, но немного не дотягивает: не хватает уникальных {unique_deficit}, "
            f"статейных {article_deficit_label}. Метрика Majestic: {metrics}.{strict_note}{note}"
        )
    else:
        verdict = "REJECT"
        status = "BAD:LOW_PROFILE"
        reason = (
            f"Профиль не достигает допуска NEAR для {locale}: нужно минимум "
            f"{near_unique}/{near_articles}, "
            f"получено {unique_quality}/{article_links_label}. Метрика Majestic: {metrics}."
        )

    return DomainVerdict(
        verdict=verdict,
        status=status,
        reason=reason,
        locale=locale,
        locale_source=locale_source,
        language=str(_value(assessment, "language", "")),
        topic=str(_value(assessment, "topic", "")),
        unique_quality=unique_quality,
        article_links=article_links,
        homepage_links=homepage_links,
        old_links=old_links,
        modern_links=modern_links,
        unknown_age_links=unknown_age_links,
        link_year_min=min(link_year_values) if link_year_values else 0,
        link_year_max=max(link_year_values) if link_year_values else 0,
        link_year_count=len(link_year_values),
        required_unique=required_unique,
        required_articles=required_articles,
        unique_deficit=unique_deficit,
        article_deficit=article_deficit,
        anchor_risk=anchor_risk,
        hard_stop_reasons=list(
            dict.fromkeys(
                hard_stops
                + ([stale_profile_stop] if stale_profile_stop else [])
                + ([homepage_hard_stop] if homepage_hard_stop else [])
            )
        ),
        warnings=warnings,
        model=model,
    )


def prepare_evidence(
    domain: str,
    title: str,
    backlinks_report: Dict[str, Any],
    fresh_anchors: Dict[str, Any],
    historic_anchors: Dict[str, Any],
    historic_pages_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    backlinks: List[Dict[str, Any]] = []
    for index, source in enumerate(backlinks_report.get("rows", []), start=1):
        row = {
            "record_id": f"M{index}",
            "source_domain": source.get("source_domain", ""),
            "source_url": source.get("source_url", ""),
            "source_title": source.get("source_title", ""),
            "source_topic": source.get("source_topic", ""),
            "language": source.get("language", ""),
            "anchor": source.get("anchor", ""),
            "source_url_tf": source.get("source_url_tf"),
            "source_url_cf": source.get("source_url_cf"),
            "source_domain_tf": source.get("source_domain_tf"),
            "source_domain_cf": source.get("source_domain_cf"),
            "outbound_external": source.get("outbound_external"),
            "external_domains": source.get("external_domains"),
            "target_url": source.get("target_url", ""),
            "target_title": source.get("target_title", ""),
            "target_topic": source.get("target_topic", ""),
            "first_indexed": source.get("first_indexed", ""),
            "last_seen": source.get("last_seen", ""),
        }
        backlinks.append(row)

    def compact_anchors(report: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            {
                "anchor": row.get("anchor", ""),
                "topic": row.get("topic", ""),
                "referring_domains": row.get("referring_domains"),
                "total_links": row.get("total_links"),
                "deleted_links": row.get("deleted_links"),
                "nofollow_links": row.get("nofollow_links"),
                "trust_flow": row.get("trust_flow"),
                "citation_flow": row.get("citation_flow"),
            }
            for row in report.get("rows", [])
        ]

    return {
        "domain": domain,
        "batch_locale_hint": title,
        "collection_contract": {
            "backlinks_index": "Fresh",
            "backlinks_follow": "DoFollow",
            "backlinks_per_ref_domain": "1",
            "backlinks_deleted": "Included",
            "anchors_indexes": ["Fresh", "Historic"],
            "historic_pages_index": "Historic" if historic_pages_report is not None else "",
            "backlinks_truncated": bool(backlinks_report.get("truncated")),
            "fresh_anchors_truncated": bool(fresh_anchors.get("truncated")),
            "historic_anchors_truncated": bool(historic_anchors.get("truncated")),
            "historic_pages_truncated": bool((historic_pages_report or {}).get("truncated")),
        },
        "backlinks": backlinks,
        "anchors_fresh": compact_anchors(fresh_anchors),
        "anchors_historic": compact_anchors(historic_anchors),
        "historic_pages": list((historic_pages_report or {}).get("rows", [])),
    }


def combine_staged_assessments(
    anchor: AnchorScreenAssessment,
    batches: Sequence[LinkBatchAssessment],
    assessed_rows: Sequence[Dict[str, Any]],
    extra_warnings: Sequence[str] = (),
) -> Dict[str, Any]:
    """Convert compact ID lists into the existing deterministic aggregation contract."""

    quality_ids: set[str] = set()
    article_ids: set[str] = set()
    half_article_ids: set[str] = set()
    old_ids: set[str] = set()
    modern_ids: set[str] = set()
    borderline_ids: set[str] = set()
    fresh_ids: set[str] = set()
    unknown_age_ids: set[str] = set()
    spam_ids: set[str] = set()
    hard_stops = list(anchor.hard_stop_reasons)
    warnings = list(anchor.warnings)
    pbn_reasons: List[str] = []
    pbn_risk = RiskLevel.CLEAN

    for batch in batches:
        quality_ids.update(map(str, batch.quality_record_ids))
        article_ids.update(map(str, batch.article_record_ids))
        half_article_ids.update(map(str, batch.half_article_record_ids))
        old_ids.update(map(str, batch.old_record_ids))
        modern_ids.update(map(str, batch.modern_record_ids))
        borderline_ids.update(map(str, batch.borderline_record_ids))
        fresh_ids.update(map(str, batch.fresh_record_ids))
        unknown_age_ids.update(map(str, batch.unknown_age_record_ids))
        spam_ids.update(map(str, batch.spam_record_ids))
        hard_stops.extend(batch.hard_stop_reasons)
        pbn_reasons.extend(batch.pbn_reasons)
        if batch.pbn_risk == RiskLevel.SPAM:
            pbn_risk = RiskLevel.SPAM
        elif batch.pbn_risk == RiskLevel.RISK and pbn_risk == RiskLevel.CLEAN:
            pbn_risk = RiskLevel.RISK

    modern_ids.update(borderline_ids)
    modern_ids.update(fresh_ids)
    valid_ids = {str(row.get("record_id")) for row in assessed_rows}
    quality_ids &= valid_ids
    article_ids &= quality_ids
    half_article_ids &= quality_ids
    half_article_ids -= article_ids
    old_ids &= valid_ids
    modern_ids &= valid_ids
    borderline_ids &= valid_ids
    fresh_ids &= valid_ids
    unknown_age_ids &= valid_ids
    spam_ids &= valid_ids
    local_nonspam_ids = {
        str(row.get("record_id") or "")
        for row in assessed_rows
        if row.get("record_id") and local_nonspam_backlink_candidate(row)
    }
    quality_ids |= local_nonspam_ids - spam_ids

    link_assessments: List[Dict[str, Any]] = []
    for row in assessed_rows:
        record_id = str(row.get("record_id") or "")
        is_spam = record_id in spam_ids
        is_quality = record_id in quality_ids and not is_spam
        is_article = record_id in article_ids and is_quality
        is_half_article = record_id in half_article_ids and is_quality and not is_article
        if record_id in old_ids:
            age_signal = AgeSignal.OLD.value
        elif record_id in modern_ids and record_id in borderline_ids:
            age_signal = AgeSignal.BORDERLINE.value
        elif record_id in modern_ids and record_id in fresh_ids:
            age_signal = AgeSignal.FRESH.value
        elif record_id in modern_ids:
            age_signal = AgeSignal.NORMAL.value
        elif record_id in unknown_age_ids:
            age_signal = AgeSignal.UNKNOWN.value
        else:
            age_signal = AgeSignal.UNKNOWN.value
        link_assessments.append(
            {
                "record_id": record_id,
                "quality": "QUALITY" if is_quality else ("SPAM" if is_spam else "UNCERTAIN"),
                "link_type": "ARTICLE" if (is_article or is_half_article) else "OTHER",
                "count_quality": is_quality,
                "count_article": is_article or is_half_article,
                "article_weight": 1.0 if is_article else (0.5 if is_half_article else 0.0),
                "prohibited_topic": "SPAM" if is_spam else "NONE",
                "age_signal": age_signal,
                "reason": "compact staged classification",
            }
        )

    warnings.extend(extra_warnings)
    return {
        "locale": anchor.locale,
        "locale_evidence": anchor.locale_evidence,
        "language": anchor.language,
        "topic": anchor.topic,
        "pbn_risk": pbn_risk.value,
        "pbn_reasons": list(dict.fromkeys(map(str, pbn_reasons))),
        "anchor_risk": anchor.anchor_risk.value,
        "anchor_reasons": anchor.anchor_reasons,
        "hard_stop_reasons": list(dict.fromkeys(map(str, hard_stops))),
        "link_assessments": link_assessments,
        "summary": anchor.summary,
        "warnings": list(dict.fromkeys(map(str, warnings))),
    }


def split_first_batch(
    value: FirstBatchAssessment,
) -> tuple[AnchorScreenAssessment, LinkBatchAssessment]:
    """Reuse the same deterministic aggregator for the combined first response."""

    anchor = AnchorScreenAssessment(
        locale=value.locale,
        locale_evidence=value.locale_evidence,
        language=value.language,
        topic=value.topic,
        anchor_risk=value.anchor_risk,
        anchor_reasons=value.anchor_reasons,
        hard_stop_reasons=value.hard_stop_reasons if value.anchor_risk == RiskLevel.SPAM else [],
        summary="",
        warnings=[],
    )
    links = LinkBatchAssessment(
        pbn_risk=value.pbn_risk,
        pbn_reasons=value.pbn_reasons,
        hard_stop_reasons=value.hard_stop_reasons,
        quality_record_ids=value.quality_record_ids,
        article_record_ids=value.article_record_ids,
        half_article_record_ids=value.half_article_record_ids,
        old_record_ids=value.old_record_ids,
        modern_record_ids=value.modern_record_ids,
        borderline_record_ids=value.borderline_record_ids,
        fresh_record_ids=value.fresh_record_ids,
        unknown_age_record_ids=value.unknown_age_record_ids,
        spam_record_ids=value.spam_record_ids,
    )
    return anchor, links


class OpenAIDomainChecker:
    def __init__(self) -> None:
        self.api_key = _env_value("OPENAI_API_KEY").strip()
        self.base_url = _env_value("OPENAI_BASE_URL").strip()
        self.screen_model = (
            _env_value("OPENAI_DOMAIN_SCREEN_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna"
        )
        self.model = _env_value("OPENAI_DOMAIN_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol"
        self.reasoning_effort = _env_value("OPENAI_REASONING_EFFORT").strip()
        self.max_output_tokens = _env_int("OPENAI_MAX_OUTPUT_TOKENS", 2500, 500, 20_000)
        self.screen_max_output_tokens = _env_int("OPENAI_SCREEN_MAX_OUTPUT_TOKENS", 900, 400, 5000)
        anchor_precheck_flag = _env_value("OPENAI_ENABLE_ANCHOR_PRECHECK").strip()
        if not anchor_precheck_flag:
            anchor_precheck_flag = _env_value("OPENAI_ENABLE_SOL_ANCHOR_PRECHECK", "1").strip()
        try:
            self.enable_sol_anchor_precheck = bool(max(0, min(int(anchor_precheck_flag), 1)))
        except (TypeError, ValueError):
            self.enable_sol_anchor_precheck = True
        self.anchor_precheck_max_output_tokens = _env_int(
            "OPENAI_ANCHOR_PRECHECK_MAX_OUTPUT_TOKENS", 700, 300, 3000
        )
        self.strict_mode = bool(_env_int("OPENAI_STRICT_NEAR_MODE", 0, 0, 1))
        self.strict_unique_deficit = _env_int("OPENAI_STRICT_NEAR_UNIQUE_DEFICIT", 1, 0, 50)
        self.strict_article_deficit = _env_int("OPENAI_STRICT_NEAR_ARTICLE_DEFICIT", 1, 0, 50)
        self.freshness_filter_enabled = bool(_env_int("OPENAI_FRESHNESS_FILTER_ENABLED", 1, 0, 1))
        self.freshness_cutoff_year = _env_int("OPENAI_FRESHNESS_CUTOFF_YEAR", 2016, 1990, 2030)
        self.freshness_max_old_share_percent = _env_int("OPENAI_FRESHNESS_MAX_OLD_SHARE", 50, 0, 100)
        self.batch_max_output_tokens = _env_int(
            "OPENAI_BATCH_MAX_OUTPUT_TOKENS", self.max_output_tokens, 600, 20_000
        )
        self.article_fallback_max_output_tokens = _env_int(
            "OPENAI_ARTICLE_FALLBACK_MAX_OUTPUT_TOKENS", 800, 200, 5000
        )
        self.batch_size = _env_int("OPENAI_LINK_BATCH_SIZE", 25, 5, 100)
        self.max_risk_anchors = _env_int("OPENAI_MAX_RISK_ANCHORS", 100, 20, 250)
        self.enable_luna_screen = bool(_env_int("OPENAI_ENABLE_LUNA_SCREEN", 0, 0, 1))
        self.fetch_page_content = bool(_env_int("OPENAI_FETCH_ARTICLE_PAGES", 1, 0, 1))
        self.max_article_pages = _env_int("OPENAI_MAX_ARTICLE_PAGES", 12, 1, 30)
        self.max_browser_article_pages = _env_int("OPENAI_BROWSER_ARTICLE_PAGES", 4, 0, 12)
        self.article_text_chars = _env_int("OPENAI_ARTICLE_TEXT_CHARS", 1200, 500, 5000)
        self.max_historic_pages_context = _env_int("OPENAI_MAX_HISTORIC_PAGES_CONTEXT", 15, 0, 50)
        self.last_error = ""
        self.model_notice = ""
        self._model_access_checked = False
        self.client = None
        if OpenAI is None:
            self.last_error = "Пакет openai не установлен"
        elif not self.api_key:
            self.last_error = "Не задана переменная OPENAI_API_KEY"
        else:
            client_options: Dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": 180.0,
                "max_retries": 2,
            }
            if self.base_url:
                client_options["base_url"] = self.base_url
            self.client = OpenAI(**client_options)

    @property
    def ready(self) -> bool:
        return self.client is not None

    def _ensure_model_access(self) -> None:
        """Validate both cascade models exposed by a third-party compatible gateway."""

        if self._model_access_checked or self.client is None:
            return
        if not self.base_url or "api.openai.com" in self.base_url.lower():
            self._model_access_checked = True
            return
        try:
            page = self.client.models.list()
            available = sorted(
                {
                    str(getattr(item, "id", "")).strip()
                    for item in getattr(page, "data", [])
                    if str(getattr(item, "id", "")).strip()
                }
            )
        except Exception:
            return
        if not available:
            return
        notices: List[str] = []
        if self.model not in available:
            model_family = ""
            for family in ("terra", "sol", "luna"):
                if family in self.model.casefold():
                    model_family = family
                    break
            same_family_models = [
                name for name in available if model_family and model_family in name.casefold()
            ]
            if same_family_models:
                previous = self.model
                self.model = same_family_models[0]
                notices.append(f"вместо {previous} для качества выбрана {self.model}")
            else:
                raise PermissionError(
                    f"Выбранная модель качества {self.model} недоступна; "
                    f"доступные модели: {', '.join(available)}"
                )
        if self.screen_model not in available:
            luna_models = [name for name in available if "luna" in name.casefold()]
            previous = self.screen_model
            if luna_models:
                self.screen_model = luna_models[0]
            else:
                self.screen_model = self.model
            notices.append(f"вместо {previous} для скрининга выбрана {self.screen_model}")
        if notices:
            self.model_notice = "Модели скорректированы автоматически: " + "; ".join(notices)
        self._model_access_checked = True

    def precheck_backlinks(
        self,
        domain: str,
        title: str,
        backlinks_report: Dict[str, Any],
    ) -> Optional[DomainVerdict]:
        """Cheap gate so the browser can skip Anchor pages and API calls."""

        evidence = prepare_evidence(domain, title, backlinks_report, {"rows": []}, {"rows": []})
        return (
            local_backlink_precheck(
                domain,
                title,
                evidence["backlinks"],
                getattr(self, "strict_mode", False),
                getattr(self, "strict_unique_deficit", 1),
                getattr(self, "strict_article_deficit", 1),
            )
            or local_source_age_precheck(
                domain,
                title,
                evidence["backlinks"],
                getattr(self, "freshness_filter_enabled", True),
                getattr(self, "freshness_cutoff_year", 2016),
                getattr(self, "freshness_max_old_share_percent", 50),
            )
        )

    def precheck_historic_pages(
        self,
        domain: str,
        title: str,
        pages_report: Dict[str, Any],
    ) -> Optional[DomainVerdict]:
        return local_historic_pages_precheck(domain, title, pages_report)

    def _parse(
        self,
        prompt: str,
        payload: Dict[str, Any],
        text_format: type[BaseModel],
        max_output_tokens: int,
        model_name: Optional[str] = None,
    ) -> tuple[BaseModel, int, int]:
        if self.client is None:
            raise RuntimeError(self.last_error or "OpenAI client is not ready")
        self._ensure_model_access()
        kwargs: Dict[str, Any] = {
            "model": model_name or self.model,
            "input": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": "Проверь только этот JSON:\n"
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "text_format": text_format,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        response = self.client.responses.parse(**kwargs)
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no parsed structured output")
        usage = getattr(response, "usage", None)
        return (
            parsed,
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )

    def evaluate(
        self,
        domain: str,
        title: str,
        backlinks_report: Dict[str, Any],
        fresh_anchors: Dict[str, Any],
        historic_anchors: Dict[str, Any],
        majestic_status: str = "GOOD",
        historic_pages_report: Optional[Dict[str, Any]] = None,
        browser_page_fetcher: Optional[Callable[[str, int], Dict[str, Any]]] = None,
    ) -> DomainVerdict:
        local_name_stop = local_domain_name_precheck(domain, title)
        if local_name_stop is not None:
            return local_name_stop
        if self.client is None:
            raise RuntimeError(self.last_error or "OpenAI client is not ready")
        screen_prompt = SCREEN_PROMPT_FILE.read_text(encoding="utf-8")
        anchor_prompt = ANCHOR_PROMPT_FILE.read_text(encoding="utf-8")
        link_prompt = PROMPT_FILE.read_text(encoding="utf-8")
        article_prompt = ARTICLE_PROMPT_FILE.read_text(encoding="utf-8")
        evidence = prepare_evidence(
            domain,
            title,
            backlinks_report,
            fresh_anchors,
            historic_anchors,
            historic_pages_report,
        )

        strict_mode = bool(getattr(self, "strict_mode", False))
        strict_unique_deficit = getattr(self, "strict_unique_deficit", 1)
        strict_article_deficit = getattr(self, "strict_article_deficit", 1)
        freshness_filter_enabled = bool(getattr(self, "freshness_filter_enabled", True))
        freshness_cutoff_year = clean_freshness_cutoff_year(
            getattr(self, "freshness_cutoff_year", 2016)
        )
        freshness_max_old_share_percent = clean_freshness_old_share_percent(
            getattr(self, "freshness_max_old_share_percent", 50)
        )
        local_threshold = local_backlink_precheck(
            domain,
            title,
            evidence["backlinks"],
            strict_mode,
            strict_unique_deficit,
            strict_article_deficit,
        )
        if local_threshold is not None:
            return local_threshold
        local_age_stop = local_source_age_precheck(
            domain,
            title,
            evidence["backlinks"],
            freshness_filter_enabled,
            freshness_cutoff_year,
            freshness_max_old_share_percent,
        )
        if local_age_stop is not None:
            return local_age_stop

        local_hard_stops = scan_anchor_hard_stops(fresh_anchors, historic_anchors)
        if local_hard_stops:
            locale, locale_source = resolve_locale_with_source(title, domain, "")
            required_unique, required_articles = thresholds_for_locale(locale)
            return DomainVerdict(
                verdict="REJECT",
                status="BAD:LOCAL_HARD_STOP",
                reason=f"Локальный жесткий стоп без запроса к API: {'; '.join(local_hard_stops)}.",
                locale=locale,
                locale_source=locale_source,
                required_unique=required_unique,
                required_articles=required_articles,
                anchor_risk=RiskLevel.SPAM.value,
                hard_stop_reasons=local_hard_stops,
                model="LOCAL_RULES",
                early_stop_stage="local_anchor_hard_stop",
            )
        local_page_stop = local_historic_pages_precheck(domain, title, historic_pages_report or {"rows": []})
        if local_page_stop is not None:
            return local_page_stop

        total_input_tokens = 0
        total_output_tokens = 0
        api_calls = 0
        anchors_sent = 0
        backlinks_sent = 0
        early_stop_stage = ""
        try:
            self._ensure_model_access()
            anchor_payload, anchor_row_count = compact_anchor_payload(evidence, self.max_risk_anchors)
            profile_mode = "GOOD_OLD" if majestic_status == "GOOD OLD" else "STANDARD"
            anchor_payload["profile_mode"] = profile_mode
            candidates = sort_backlinks_for_ai(evidence["backlinks"])
            critical_candidates = sort_backlinks_for_critical(evidence["backlinks"])
            locale = resolve_locale(title, domain, "")
            required_unique, required_articles = thresholds_for_locale(locale)
            near_unique, near_articles = near_thresholds_for_locale(
                locale,
                strict_mode,
                strict_unique_deficit,
                strict_article_deficit,
            )
            total_batches = max(1, (len(candidates) + self.batch_size - 1) // self.batch_size)
            batches: List[LinkBatchAssessment] = []
            anchor_assessment: Optional[AnchorScreenAssessment] = None
            assessed_rows: List[Dict[str, Any]] = []
            quality_ids: set[str] = set()
            article_ids: set[str] = set()
            half_article_ids: set[str] = set()
            modern_quality_ids: set[str] = set()
            source_modern_quality_ids: set[str] = set()
            old_quality_ids: set[str] = set()
            homepage_quality_ids: set[str] = set()
            browser_fallback_rows: Dict[str, Dict[str, Any]] = {}
            extra_warnings: List[str] = []
            if not anchor_row_count:
                extra_warnings.append("Majestic не вернул Anchor Text")

            screen_rows = critical_candidates[: self.batch_size]
            screen_payload = compact_critical_payload(domain, title, screen_rows)
            screen_payload["profile_mode"] = profile_mode
            screen_payload["anchors"] = anchor_payload
            if getattr(self, "enable_luna_screen", True):
                parsed_screen, input_tokens, output_tokens = self._parse(
                    screen_prompt,
                    screen_payload,
                    CriticalScreenAssessment,
                    self.screen_max_output_tokens,
                    self.screen_model,
                )
                critical_screen = CriticalScreenAssessment.model_validate(parsed_screen)
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                api_calls += 1
                backlinks_sent += len(screen_rows)
                anchors_sent += anchor_row_count
            else:
                critical_screen = CriticalScreenAssessment(
                    anchor_risk=RiskLevel.CLEAN,
                    pbn_risk=RiskLevel.CLEAN,
                    hard_stop_reasons=[],
                )

            critical_reasons = [
                reason
                for reason in critical_screen.hard_stop_reasons
                if is_independent_critical_reason(reason)
            ]
            ignored_screen_reasons = [
                reason
                for reason in critical_screen.hard_stop_reasons
                if not is_independent_critical_reason(reason)
            ]
            critical_reasons = list(dict.fromkeys(str(x).strip() for x in critical_reasons if str(x).strip()))
            confirmed_luna_stop = (
                critical_screen.pbn_risk == RiskLevel.SPAM and bool(critical_reasons)
            )
            if ignored_screen_reasons or (
                not confirmed_luna_stop
                and (
                    critical_screen.anchor_risk == RiskLevel.SPAM
                    or critical_screen.pbn_risk == RiskLevel.SPAM
                )
            ):
                extra_warnings.append(
                    "Быстрый скрининг не получил права на hard stop: нет одновременно независимой критической причины и подтверждённого SPAM-риска переиспользования/PBN; профиль передан выбранной модели"
            )
            if confirmed_luna_stop:
                locale, locale_source = resolve_locale_with_source(title, domain, "")
                required_unique, required_articles = thresholds_for_locale(locale)
                self.last_error = ""
                return DomainVerdict(
                    verdict="REJECT",
                    status="BAD:AI_HARD_STOP",
                    reason=(
                        "Быстрый скрининг обнаружил уверенный критический стоп; полная AI-проверка не вызывалась: "
                        + "; ".join(critical_reasons)
                        + "."
                    ),
                    locale=locale,
                    locale_source=locale_source,
                    required_unique=required_unique,
                    required_articles=required_articles,
                    anchor_risk=critical_screen.anchor_risk.value,
                    hard_stop_reasons=critical_reasons,
                    model=self.screen_model,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    api_calls=api_calls,
                    backlinks_sent=backlinks_sent,
                    anchors_sent=anchors_sent,
                    early_stop_stage="luna_critical_screen",
                )
            if (
                critical_screen.anchor_risk == RiskLevel.RISK
                or critical_screen.pbn_risk == RiskLevel.RISK
            ):
                extra_warnings.append("Быстрый скрининг отметил предварительный риск; выбранная модель выполнила полную перепроверку")

            sol_anchor_precheck_reasons = anchor_precheck_suspicion_reasons(
                fresh_anchors,
                historic_anchors,
                domain,
                title,
            )
            if (
                getattr(self, "enable_sol_anchor_precheck", True)
                and not getattr(self, "enable_luna_screen", True)
                and anchor_row_count
                and sol_anchor_precheck_reasons
            ):
                precheck_payload = dict(anchor_payload)
                precheck_payload["precheck_reasons"] = sol_anchor_precheck_reasons
                parsed_anchor, input_tokens, output_tokens = self._parse(
                    anchor_prompt
                    + "\n\nЭто короткая предварительная проверка только anchors. "
                    "Backlinks/pages не переданы. Верни только оценку анкорного профиля; "
                    "не вычисляй итоговый GOOD/BAD домена.",
                    precheck_payload,
                    AnchorScreenAssessment,
                    getattr(self, "anchor_precheck_max_output_tokens", 700),
                    self.model,
                )
                precheck_anchor = AnchorScreenAssessment.model_validate(parsed_anchor)
                precheck_anchor, anchor_warnings = sanitize_seo_only_anchor(precheck_anchor)
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                api_calls += 1
                anchors_sent += anchor_row_count
                extra_warnings.extend(anchor_warnings)
                if (
                    precheck_anchor.anchor_risk == RiskLevel.SPAM
                    and precheck_anchor.hard_stop_reasons
                ):
                    locale, locale_source = resolve_locale_with_source(title, domain, precheck_anchor.locale)
                    required_unique, required_articles = thresholds_for_locale(locale)
                    self.last_error = ""
                    return DomainVerdict(
                        verdict="REJECT",
                        status="BAD:AI_HARD_STOP",
                        reason=(
                            "Anchor-only AI подтвердил жёсткий стоп до полной проверки ссылок: "
                            + "; ".join(precheck_anchor.hard_stop_reasons)
                            + "."
                        ),
                        locale=locale,
                        locale_source=locale_source,
                        language=precheck_anchor.language,
                        topic=precheck_anchor.topic,
                        required_unique=required_unique,
                        required_articles=required_articles,
                        anchor_risk=precheck_anchor.anchor_risk.value,
                        hard_stop_reasons=precheck_anchor.hard_stop_reasons,
                        warnings=extra_warnings,
                        model=self.model,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        api_calls=api_calls,
                        backlinks_sent=backlinks_sent,
                        anchors_sent=anchors_sent,
                        early_stop_stage="sol_anchor_precheck",
                    )
                if precheck_anchor.anchor_risk == RiskLevel.CLEAN:
                    anchor_assessment = precheck_anchor
                    extra_warnings.append(
                        "Anchor-only AI не нашёл стопов; anchors не дублируются в первом полном запросе"
                    )
                else:
                    extra_warnings.append(
                        "Anchor-only AI не подтвердил жёсткий стоп; профиль передан в полную проверку выбранной моделью"
                    )
            batch_prompt = link_prompt + "\n\nПРАВИЛА ПРОВЕРКИ СОДЕРЖИМОГО ОТКРЫТЫХ СТРАНИЦ:\n" + article_prompt
            first_prompt = (
                anchor_prompt
                + "\n\nВ ЭТОМ ЖЕ ОТВЕТЕ классифицируй первую пачку ссылок по следующим правилам:\n"
                + batch_prompt
            )

            for offset in range(0, len(candidates), self.batch_size):
                batch_rows = candidates[offset : offset + self.batch_size]
                batch_number = offset // self.batch_size + 1
                payload = compact_backlink_batch(
                    domain,
                    title,
                    batch_rows,
                    batch_number,
                    total_batches,
                )
                payload["profile_mode"] = profile_mode
                page_candidate_ids: List[str] = []
                for row in batch_rows:
                    try:
                        source_path = urlparse(str(row.get("source_url") or "")).path or "/"
                    except ValueError:
                        source_path = "/"
                    if source_path not in {"", "/"}:
                        page_candidate_ids.append(str(row.get("record_id") or ""))
                if getattr(self, "fetch_page_content", False):
                    page_evidence, skipped_pages = collect_article_page_evidence(
                        batch_rows,
                        page_candidate_ids,
                        max_pages=getattr(self, "max_article_pages", 12),
                        max_chars=getattr(self, "article_text_chars", 1200),
                    )
                else:
                    page_evidence, skipped_pages = [], 0
                accessible_page_ids = {
                    str(page.get("id"))
                    for page in page_evidence
                    if page.get("fetch_status") == "OK" and str(page.get("text_excerpt") or "").strip()
                }
                page_years_by_id = {
                    str(page.get("id")): extract_explicit_years(
                        page.get("page_title"),
                        page.get("description"),
                    )
                    for page in page_evidence
                }
                for row in batch_rows:
                    years = page_years_by_id.get(str(row.get("record_id") or ""))
                    if years:
                        row["page_years"] = years
                payload["page_columns"] = [
                    "id",
                    "fetch_status",
                    "http_status",
                    "page_title",
                    "description",
                    "text_excerpt",
                    "target_link_found",
                    "target_link_texts",
                    "link_dom_area",
                    "link_context_excerpt",
                    "external_links_count",
                    "visible_text_chars",
                    "external_link_density",
                ]
                payload["pages"] = [
                    [
                        page.get("id"),
                        page.get("fetch_status"),
                        page.get("http_status"),
                        page.get("page_title"),
                        page.get("description"),
                        page.get("text_excerpt"),
                        page.get("target_link_found"),
                        page.get("target_link_texts"),
                        page.get("link_dom_area"),
                        page.get("link_context_excerpt"),
                        page.get("external_links_count"),
                        page.get("visible_text_chars"),
                        page.get("external_link_density"),
                    ]
                    for page in page_evidence
                ]
                failed_pages = len(page_evidence) - len(accessible_page_ids)
                if failed_pages:
                    extra_warnings.append(
                        f"Не удалось прочитать потенциально статейных страниц: {failed_pages}; они не засчитаны"
                    )
                if skipped_pages:
                    extra_warnings.append(
                        f"Лимит проверки содержимого: не открыто потенциальных страниц {skipped_pages}"
                    )
                if batch_number == 1:
                    page_columns, historic_pages, historic_pages_truncated = compact_historic_pages_payload(
                        {"rows": evidence.get("historic_pages", []), "truncated": evidence["collection_contract"].get("historic_pages_truncated")},
                        max_rows=getattr(self, "max_historic_pages_context", 15),
                    )
                    if historic_pages:
                        payload["historic_pages_columns"] = page_columns
                        payload["historic_pages"] = historic_pages
                        payload["historic_pages_truncated"] = historic_pages_truncated
                    if anchor_assessment is None:
                        payload["anchors"] = anchor_payload
                        anchors_sent += anchor_row_count
                        parsed_first, input_tokens, output_tokens = self._parse(
                            first_prompt,
                            payload,
                            FirstBatchAssessment,
                            self.batch_max_output_tokens,
                        )
                        first_assessment = FirstBatchAssessment.model_validate(parsed_first)
                        anchor_assessment, batch_assessment = split_first_batch(first_assessment)
                        anchor_assessment, anchor_warnings = sanitize_seo_only_anchor(anchor_assessment)
                        batch_assessment, batch_warnings = sanitize_seo_only_batch(batch_assessment)
                        extra_warnings.extend(anchor_warnings)
                        extra_warnings.extend(batch_warnings)
                    else:
                        parsed_batch, input_tokens, output_tokens = self._parse(
                            batch_prompt,
                            payload,
                            LinkBatchAssessment,
                            self.batch_max_output_tokens,
                        )
                        batch_assessment = LinkBatchAssessment.model_validate(parsed_batch)
                        batch_assessment, batch_warnings = sanitize_seo_only_batch(batch_assessment)
                        extra_warnings.extend(batch_warnings)
                    locale = resolve_locale(title, domain, anchor_assessment.locale)
                    required_unique, required_articles = thresholds_for_locale(locale)
                    near_unique, near_articles = near_thresholds_for_locale(
                        locale,
                        strict_mode,
                        strict_unique_deficit,
                        strict_article_deficit,
                    )
                else:
                    parsed_batch, input_tokens, output_tokens = self._parse(
                        batch_prompt,
                        payload,
                        LinkBatchAssessment,
                        self.batch_max_output_tokens,
                    )
                    batch_assessment = LinkBatchAssessment.model_validate(parsed_batch)
                    batch_assessment, batch_warnings = sanitize_seo_only_batch(batch_assessment)
                    extra_warnings.extend(batch_warnings)
                assessed_rows.extend(batch_rows)
                backlinks_sent += len(batch_rows)
                api_calls += 1
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                valid_batch_ids = {str(row.get("record_id")) for row in batch_rows}
                spam_ids = set(map(str, batch_assessment.spam_record_ids)) & valid_batch_ids
                accepted_quality = (
                    set(map(str, batch_assessment.quality_record_ids)) & valid_batch_ids
                ) - spam_ids
                local_quality_ids = {
                    str(row.get("record_id") or "")
                    for row in batch_rows
                    if row.get("record_id") and local_nonspam_backlink_candidate(row)
                } - spam_ids
                article_quality_ids = accepted_quality | local_quality_ids
                verified_article_ids = (
                    set(map(str, batch_assessment.article_record_ids)) & article_quality_ids
                    & accessible_page_ids
                )
                verified_half_article_ids = (
                    set(map(str, batch_assessment.half_article_record_ids)) & article_quality_ids
                    & accessible_page_ids
                ) - verified_article_ids
                batch_assessment = batch_assessment.model_copy(
                    update={
                        "article_record_ids": sorted(verified_article_ids),
                        "half_article_record_ids": sorted(verified_half_article_ids),
                    }
                )
                fallback_source_ids = set(map(str, page_candidate_ids))
                for row in batch_rows:
                    record_id = str(row.get("record_id") or "")
                    if (
                        browser_page_fetcher is not None
                        and record_id in article_quality_ids
                        and record_id in fallback_source_ids
                        and record_id not in accessible_page_ids
                        and record_id not in browser_fallback_rows
                    ):
                        browser_fallback_rows[record_id] = row
                batches.append(batch_assessment)
                quality_ids.update(accepted_quality)
                article_ids.update(verified_article_ids)
                half_article_ids.update(verified_half_article_ids)
                model_modern_ids = (
                    set(map(str, batch_assessment.modern_record_ids))
                    | set(map(str, batch_assessment.borderline_record_ids))
                    | set(map(str, batch_assessment.fresh_record_ids))
                ) & accepted_quality
                modern_quality_ids.update(model_modern_ids)
                for row in batch_rows:
                    record_id = str(row.get("record_id") or "")
                    if record_id not in accepted_quality:
                        continue
                    age_bucket = backlink_age_bucket(row, freshness_cutoff_year)
                    if age_bucket == "old":
                        old_quality_ids.add(record_id)
                        modern_quality_ids.discard(record_id)
                    elif age_bucket == "modern":
                        source_modern_quality_ids.add(record_id)
                        modern_quality_ids.add(record_id)
                homepage_quality_ids.update(
                    record_id
                    for record_id in accepted_quality
                    if any(
                        str(row.get("record_id")) == record_id
                        and is_exact_homepage(str(row.get("target_url") or ""), domain)
                        for row in batch_rows
                    )
                )

                if anchor_assessment is not None and anchor_assessment.anchor_risk != RiskLevel.CLEAN:
                    early_stop_stage = "anchor_screen"
                    break

                if batch_assessment.hard_stop_reasons or batch_assessment.pbn_risk != RiskLevel.CLEAN:
                    early_stop_stage = "backlink_risk"
                    break

                remaining_rows = candidates[len(assessed_rows) :]
                remaining = len(remaining_rows)
                remaining_home = sum(
                    1
                    for row in remaining_rows
                    if is_exact_homepage(str(row.get("target_url") or ""), domain)
                )
                remaining_non_home = remaining - remaining_home
                remaining_quality_candidates = sum(
                    1 for row in remaining_rows if local_nonspam_backlink_candidate(row)
                )
                freshness_safe_for_early_exit = True
                if freshness_filter_enabled and old_quality_ids:
                    projected_quality = max(1, len(quality_ids) + remaining_quality_candidates)
                    projected_old = len(old_quality_ids) + remaining_quality_candidates
                    freshness_safe_for_early_exit = (
                        bool(source_modern_quality_ids)
                        and 100 * projected_old <= freshness_max_old_share_percent * projected_quality
                    )

                # Best case: every remaining homepage row is quality and every
                # remaining inner row is not. If that still cannot reach 50%,
                # the deterministic homepage stop is already final.
                if quality_ids and (
                    2 * (len(homepage_quality_ids) + remaining_home)
                    < len(quality_ids) + remaining_home
                ):
                    early_stop_stage = "homepage_share_impossible"
                    if remaining:
                        extra_warnings.append(
                            "Ранний выход: даже все оставшиеся ссылки на главную не поднимут долю до 50%"
                        )
                    break

                if (
                    len(quality_ids) >= required_unique
                    and _ceil_metric_number(len(article_ids) + min(2.0, len(half_article_ids) * 0.5)) >= required_articles
                    and freshness_safe_for_early_exit
                ):
                    # Do not accept a partial profile whose unprocessed inner
                    # links could still push the final homepage share below 50%.
                    if (
                        2 * len(homepage_quality_ids)
                        >= len(quality_ids) + remaining_non_home
                    ):
                        early_stop_stage = "strict_threshold_reached"
                        if remaining:
                            extra_warnings.append(
                                f"Ранний выход: строгий порог и доля главной гарантированы; "
                                f"не отправлено ссылок: {remaining}"
                            )
                        break

                if (
                    len(quality_ids) + remaining < near_unique
                    or _ceil_metric_number(len(article_ids) + min(2.0, len(half_article_ids) * 0.5) + remaining) < near_articles
                ):
                    early_stop_stage = "near_threshold_impossible"
                    if remaining:
                        extra_warnings.append(
                            f"Ранний выход: даже все оставшиеся {remaining} ссылок не позволяют достичь допуска"
                        )
                    break

            if anchor_assessment is None:
                raise RuntimeError("First batch did not produce an anchor assessment")

            browser_blocking_stage = early_stop_stage in {
                "anchor_screen",
                "backlink_risk",
                "homepage_share_impossible",
            }
            browser_limit = int(getattr(self, "max_browser_article_pages", 0) or 0)
            browser_attempts = min(browser_limit, len(browser_fallback_rows))
            can_browser_reach_good = (
                required_articles > 0
                and _ceil_metric_number(len(article_ids) + min(2.0, len(half_article_ids) * 0.5)) < required_articles
                and _ceil_metric_number(len(article_ids) + min(2.0, len(half_article_ids) * 0.5) + browser_attempts) >= required_articles
            )
            can_browser_reach_near = (
                required_articles > 0
                and _ceil_metric_number(len(article_ids) + min(2.0, len(half_article_ids) * 0.5)) < near_articles
                and _ceil_metric_number(len(article_ids) + min(2.0, len(half_article_ids) * 0.5) + browser_attempts) >= near_articles
            )
            if (
                browser_page_fetcher is not None
                and browser_attempts > 0
                and not browser_blocking_stage
                and (can_browser_reach_good or can_browser_reach_near)
            ):
                fallback_rows = list(browser_fallback_rows.values())[:browser_limit]
                fallback_ids = [str(row.get("record_id") or "") for row in fallback_rows]
                browser_pages, browser_skipped = collect_browser_article_page_evidence(
                    fallback_rows,
                    fallback_ids,
                    browser_page_fetcher,
                    max_pages=browser_limit,
                    max_chars=getattr(self, "article_text_chars", 1200),
                )
                if browser_skipped:
                    extra_warnings.append(
                        f"Browser fallback: не открыто страниц из-за лимита: {browser_skipped}"
                    )
                browser_accessible_ids = {
                    str(page.get("id"))
                    for page in browser_pages
                    if page.get("fetch_status") == "OK" and str(page.get("text_excerpt") or "").strip()
                }
                browser_page_years_by_id = {
                    str(page.get("id")): extract_explicit_years(
                        page.get("page_title"),
                        page.get("description"),
                        page.get("text_excerpt"),
                    )
                    for page in browser_pages
                }
                for row in fallback_rows:
                    years = browser_page_years_by_id.get(str(row.get("record_id") or ""))
                    if years:
                        row["page_years"] = years
                if browser_accessible_ids:
                    fallback_payload = compact_backlink_batch(
                        domain,
                        title,
                        fallback_rows,
                        1,
                        1,
                    )
                    fallback_payload["profile_mode"] = profile_mode
                    fallback_payload["page_columns"] = [
                        "id",
                        "fetch_status",
                        "http_status",
                        "page_title",
                        "description",
                        "text_excerpt",
                        "target_link_found",
                        "target_link_texts",
                        "link_dom_area",
                        "link_context_excerpt",
                        "external_links_count",
                        "visible_text_chars",
                        "external_link_density",
                    ]
                    fallback_payload["pages"] = [
                        [
                            page.get("id"),
                            page.get("fetch_status"),
                            page.get("http_status"),
                            page.get("page_title"),
                            page.get("description"),
                            page.get("text_excerpt"),
                            page.get("target_link_found"),
                            page.get("target_link_texts"),
                            page.get("link_dom_area"),
                            page.get("link_context_excerpt"),
                            page.get("external_links_count"),
                            page.get("visible_text_chars"),
                            page.get("external_link_density"),
                        ]
                        for page in browser_pages
                    ]
                    parsed_fallback, input_tokens, output_tokens = self._parse(
                        "Повторная проверка статейности после браузерного открытия страниц. "
                        "Верни article_record_ids для полноценных статей и half_article_record_ids для слабых 0.5 статейных упоминаний.\n\n"
                        + article_prompt
                        + "\n\nReturn article_record_ids for full 1.0 article links and half_article_record_ids for borderline 0.5 article links.",
                        fallback_payload,
                        ArticleFallbackAssessment,
                        getattr(self, "article_fallback_max_output_tokens", 800),
                    )
                    fallback_assessment = ArticleFallbackAssessment.model_validate(parsed_fallback)
                    fallback_quality_ids = {
                        str(row.get("record_id") or "")
                        for row in fallback_rows
                        if row.get("record_id") and local_nonspam_backlink_candidate(row)
                    }
                    verified_browser_article_ids = (
                        set(map(str, fallback_assessment.article_record_ids))
                        & fallback_quality_ids
                        & browser_accessible_ids
                    )
                    verified_browser_half_article_ids = (
                        set(map(str, fallback_assessment.half_article_record_ids))
                        & fallback_quality_ids
                        & browser_accessible_ids
                    ) - verified_browser_article_ids
                    if verified_browser_article_ids or verified_browser_half_article_ids:
                        batches.append(
                            LinkBatchAssessment(
                                pbn_risk=RiskLevel.CLEAN,
                                pbn_reasons=[],
                                hard_stop_reasons=[],
                                quality_record_ids=sorted(fallback_quality_ids),
                                article_record_ids=sorted(verified_browser_article_ids),
                                half_article_record_ids=sorted(verified_browser_half_article_ids),
                                old_record_ids=[],
                                modern_record_ids=[],
                                borderline_record_ids=[],
                                fresh_record_ids=[],
                                unknown_age_record_ids=sorted(fallback_quality_ids),
                                spam_record_ids=[],
                            )
                        )
                        article_ids.update(verified_browser_article_ids)
                        half_article_ids.update(verified_browser_half_article_ids)
                        early_stop_stage = "browser_article_fallback"
                        extra_warnings.append(
                            f"Browser fallback подтвердил статейные ссылки: {len(verified_browser_article_ids)}"
                        )
                    else:
                        extra_warnings.append("Browser fallback не подтвердил дополнительных статейных ссылок")
                    api_calls += 1
                    backlinks_sent += len(fallback_rows)
                    total_input_tokens += input_tokens
                    total_output_tokens += output_tokens
                else:
                    extra_warnings.append("Browser fallback не смог получить текст спорных страниц")

            staged = combine_staged_assessments(
                anchor_assessment,
                batches,
                assessed_rows,
                extra_warnings,
            )
            verdict = aggregate_assessment(
                domain=domain,
                title=title,
                backlinks=assessed_rows,
                assessment=staged,
                model=(
                    self.model
                    if not getattr(self, "enable_luna_screen", True) or self.screen_model == self.model
                    else f"{self.screen_model} → {self.model}"
                ),
                strict_mode=strict_mode,
                strict_unique_deficit=strict_unique_deficit,
                strict_article_deficit=strict_article_deficit,
                freshness_filter_enabled=freshness_filter_enabled,
                freshness_cutoff_year=freshness_cutoff_year,
                freshness_max_old_share_percent=freshness_max_old_share_percent,
            )
            verdict.input_tokens = total_input_tokens
            verdict.output_tokens = total_output_tokens
            verdict.api_calls = api_calls
            verdict.backlinks_sent = backlinks_sent
            verdict.anchors_sent = anchors_sent
            verdict.early_stop_stage = early_stop_stage or "all_candidates_checked"
            self.last_error = ""
            return verdict
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
