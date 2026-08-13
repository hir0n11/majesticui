"""Small Wayback/WebArchive spam gate used before accepting GOOD domains.

The checker intentionally stays deterministic: it uses Wayback CDX metadata,
downloads a bounded number of archived HTML snapshots, extracts visible text,
and scans it with high-confidence spam-topic regexes.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import os
from pathlib import Path
from typing import Any, List, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_WEB_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
CJK_LOCALES = {"CN", "JP", "KR", "TW", "HK", "MO"}
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
JAPANESE_RE = re.compile(r"[\u3040-\u30ff]")
KOREAN_RE = re.compile(r"[\uac00-\ud7af]")
BENGALI_RE = re.compile(r"[\u0980-\u09ff]")
THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
SCRIPT_FILTERS: Sequence[tuple[str, re.Pattern[str], set[str], str]] = (
    ("chinese characters", CHINESE_RE, CJK_LOCALES, "Chinese text"),
    ("korean characters", KOREAN_RE, {"KR"}, "Korean text"),
    ("japanese characters", JAPANESE_RE, {"JP"}, "Japanese text"),
    ("bengali characters", BENGALI_RE, {"BD", "BN"}, "Bengali text"),
    ("thai characters", THAI_RE, {"TH"}, "Thai text"),
    ("hindi/devanagari", DEVANAGARI_RE, {"IN", "HI", "NP"}, "Hindi/Devanagari text"),
)
DEFAULT_SCRIPT_MIN_CHARS = 20


@dataclass
class WaybackSnapshot:
    timestamp: str
    original: str
    digest: str = ""
    statuscode: str = ""
    mimetype: str = ""

    @property
    def month(self) -> str:
        return self.timestamp[:6]

    @property
    def display_month(self) -> str:
        value = self.timestamp
        return f"{value[:4]}-{value[4:6]}" if len(value) >= 6 else value


@dataclass
class WebArchiveSpamMatch:
    category: str
    sample: str
    count: int


@dataclass
class WebArchiveSpamHit:
    timestamp: str
    original: str
    title: str = ""
    matches: List[WebArchiveSpamMatch] = field(default_factory=list)

    @property
    def display_month(self) -> str:
        value = self.timestamp
        return f"{value[:4]}-{value[4:6]}" if len(value) >= 6 else value


@dataclass
class WebArchiveLocaleSample:
    timestamp: str
    original: str
    title: str
    excerpt: str
    confidence: str = "LOW"
    life_start: str = ""
    life_end: str = ""
    supporting_snapshots: int = 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "original": self.original,
            "title": self.title,
            "excerpt": self.excerpt,
            "confidence": self.confidence,
            "life_period": (
                self.life_start
                if self.life_start == self.life_end
                else f"{self.life_start}-{self.life_end}".strip("-")
            ),
            "supporting_snapshots": self.supporting_snapshots,
        }


@dataclass
class WebArchiveScriptObservation:
    timestamp: str
    original: str
    title: str = ""
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class WebArchivePageContent:
    snapshot: WaybackSnapshot
    title: str
    text: str


@dataclass
class WebArchiveSpamResult:
    checked: bool
    spam: bool
    snapshots_found: int = 0
    snapshots_checked: int = 0
    hits: List[WebArchiveSpamHit] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    locale_samples: List[WebArchiveLocaleSample] = field(default_factory=list)
    script_observations: List[WebArchiveScriptObservation] = field(default_factory=list)
    no_life_found: bool = False

    @property
    def reason(self) -> str:
        if self.spam and self.hits:
            parts = []
            for hit in self.hits[:3]:
                match_text = ", ".join(
                    f"{match.category}: «{match.sample}»"
                    for match in hit.matches[:3]
                )
                title = f", title «{hit.title[:90]}»" if hit.title else ""
                parts.append(f"{hit.display_month}{title}: {match_text}")
            return (
                f"WebArchive spam: найден запрещённый/рисковый контент в "
                f"{len(self.hits)} из {self.snapshots_checked} проверенных snapshot. "
                + "; ".join(parts)
            )
        if self.checked:
            return f"WebArchive: спам не найден в {self.snapshots_checked} snapshot."
        if self.no_life_found:
            return "WebArchive: no representative non-placeholder site life was found."
        if self.errors:
            return "WebArchive не проверен: " + "; ".join(self.errors[:3])
        return "WebArchive не проверен: нет доступных HTML snapshot."


class _VisibleArchiveParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data or "").strip()
        if not value:
            return
        if self.in_title:
            self.title_parts.append(value)
            return
        if not self.skip_depth:
            self.text_parts.append(value)


SPAM_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "english spam words",
        re.compile(
            r"\b(?:"
            r"viagra|cialis|levitra|kamagra|sildenafil|tadalafil|phentermine|"
            r"casino|gambling|slots?|poker|blackjack|roulette|togel|cakep\s*togel|sportsbook|bookmaker|"
            r"forex|binary\s+options?|metatrader|crypto(?:currency)?|bitcoin|blockchain|ethereum|nft|"
            r"payday\s+loans?|cash\s+advance|bad\s+credit\s+loans?|"
            r"escort(?:s)?|porn|porno|xxx|hentai|adult\s+dating|sex\s*cam|onlyfans|"
            r"cheap\s+(?:jerseys?|viagra|cialis|essay|shoes?|bags?|sneakers?)|"
            r"replica\s+(?:watch|watches|bags?|handbags?|jerseys?|shoes?)|"
            r"buy\s+(?:essay|backlinks?|pills|viagra|cialis)|"
            r"essay\s+writing|assignment\s+help|dissertation\s+(?:help|writing)|"
            r"aged\s+domains?|expired\s+domains?|pbn\s+(?:links?|network|service)"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "russian spam words",
        re.compile(
            r"(?<![\w])(?:"
            r"казино|онлайн\s*казино|слоты|игровые\s+автоматы|ставки\s+на\s+спорт|букмекер|"
            r"виагра|сиалис|левитра|камасутра|купить\s+(?:виагру|сиалис|таблетки)|"
            r"форекс|бинарные\s+опционы|крипто(?:валюта)?|биткоин|эфириум|"
            r"займ(?:ы)?|микрозайм(?:ы)?|кредит(?:ы)?\s+без\s+отказа|"
            r"эскорт|порно|секс\s*чат|вебкам|"
            r"купить\s+ссылки|seo\s+ссылки|pbn|дорвей|дорвеи|"
            r"диплом\s+на\s+заказ|курсовая\s+на\s+заказ|реферат\s+на\s+заказ"
            r")(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (
        "pharma",
        re.compile(
            r"\b(?:viagra|cialis|levitra|kamagra|sildenafil|tadalafil|phentermine|"
            r"provigil|priligy|online\s+pharmacy|generic\s+(?:viagra|cialis)|"
            r"buy\s+(?:pills|viagra|cialis|levitra|kamagra))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "multilingual pharma",
        re.compile(
            r"\b(?:farmacia\s+online|comprar\s+(?:viagra|cialis|kamagra)|"
            r"(?:viagra|cialis|kamagra)\s+sin\s+receta|"
            r"pharmacie\s+en\s+ligne|acheter\s+(?:viagra|cialis|kamagra)|"
            r"(?:viagra|cialis|kamagra)\s+sans\s+ordonnance|"
            r"apotheke\s+rezeptfrei|(?:viagra|cialis|kamagra)\s+kaufen|"
            r"farmacia\s+senza\s+ricetta|comprare\s+(?:viagra|cialis|kamagra)|"
            r"apteka\s+internetowa|(?:viagra|cialis|kamagra)\s+bez\s+recepty)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "casino/betting",
        re.compile(
            r"\b(?:online\s+casino|casino\s+bonus|casino|kasino|gambling|slots?|"
            r"slot\s+gacor|poker|blackjack|roulette|togel|sportsbook|bookmaker|"
            r"1xbet|22bet|20bet|bet365|melbet|dafabet|pinup\s*bet|kubet)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "multilingual gambling",
        re.compile(
            r"(?:"
            r"\b(?:sbobet|ibcbet|maxbet|cmd368|playme8|cakep\s*togel|judi\s+online|judi\s+(?:bola|slot)|"
            r"situs\s+(?:judi|slot|togel)|bandar\s+(?:judi|togel)|agen\s+(?:sbobet|bola|togel)|"
            r"taruhan\s+(?:online|bola)|slot\s+gacor|togel\s+online|"
            r"daftar\s+(?:sbobet|slot|judi|togel)|login\s+(?:sbobet|slot|judi|togel)|"
            r"link\s+alternatif\s+(?:sbobet|slot|judi|togel)|"
            r"(?:deposit\s+pulsa.{0,40}(?:slot|togel|judi)|(?:slot|togel|judi).{0,40}deposit\s+pulsa))\b|"
            r"\b(?:nha\s+cai|ca\s+cuoc|danh\s+bac|tai\s+xiu|xoc\s+dia|lo\s+de|"
            r"soi\s+keo|keo\s+nha\s+cai|ca\s+do\s+bong\s+da)\b|"
            r"\b(?:nhà\s+cái|cá\s+cược|đánh\s+bạc|tài\s+xỉu|xóc\s+đĩa|lô\s+đề|"
            r"soi\s+kèo|kèo\s+nhà\s+cái|cá\s+độ\s+bóng\s+đá)\b|"
            r"\b(?:casino\s+en\s+l[ií]nea|apuestas\s+deportivas|casa\s+de\s+apuestas|"
            r"cassino\s+online|apostas\s+esportivas|casa\s+de\s+apostas|"
            r"casino\s+en\s+ligne|paris\s+sportifs|"
            r"casin[oò]\s+online|scommesse\s+sportive|"
            r"kasyno\s+online|online\s+kasyno|zakłady\s+bukmacherskie|zaklady\s+bukmacherskie|"
            r"wettanbieter|sportwetten|spielautomaten)\b|"
            r"(?:พนันออนไลน์|เว็บสล็อต|สล็อตแตกง่าย|แทงบอล|บาคาร่า|คาสิโนออนไลน์|หวยออนไลน์|"
            r"토토사이트|온라인카지노|스포츠토토|바카라|슬롯머신|"
            r"在线博彩|網上博彩|网上赌场|在線賭場|在线赌场|真人娱乐城)"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "adult",
        re.compile(
            r"\b(?:porn|porno|xxx|hentai|escort(?:s)?|adult\s+dating|sex\s*cam|"
            r"webcam\s*sex|onlyfans|call\s+girls?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "adult/erotic content",
        re.compile(
            r"\b(?:erotikads|erotik\s*ads|erotische\s+massage|erotic\s+massage|sensual\s+massage|"
            r"sexspielzeug(?:e|en)?|sexuelle\s+fantasien|sexuelle\s+bed[üu]rfnisse|"
            r"sexuelle\s+verbindung|sexuelle\s+beziehung|naked\s+body)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forex/trading",
        re.compile(
            r"\b(?:forex|binary\s+options?|metatrader|mt4|mt5|cfd\s+trading|"
            r"forex\s+(?:broker|signals?|robot|bonus)|online\s+trading)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "crypto/nft",
        re.compile(
            r"\b(?:crypto(?:currency)?|bitcoin|bitcoins|blockchain|ethereum|litecoin|"
            r"dogecoin|altcoin|memecoin|airdrop|binance|coinbase|web3|metaverse|"
            r"nft(?:s)?|token\s+(?:sale|airdrop|swap)|coin\s+(?:market|wallet|trading)|"
            r"(?:crypto|krypto)\s*(?:signals?|signale|trading|investment|investing)|"
            r"(?:bitcoin|btc)\s*(?:signals?|trading|investment|investing)|"
            r"[a-z0-9]{2,}coins?\b.{0,160}\b(?:credits?|credittek|tokens?|rewards?|wallet|app|earn|spent|"
            r"vásárolható|kapsz|ft-nyi|fenntarthat[óo])\b|"
            r"\b(?:credits?|credittek|tokens?|rewards?|wallet|app|earn|spent|vásárolható|kapsz|ft-nyi|"
            r"fenntarthat[óo])\b.{0,160}\b[a-z0-9]{2,}coins?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "japanese sidejob/investment spam",
        re.compile(
            r"(?:"
            r"ネット副業|スマホ副業|おすすめネット副業|"
            r"(?:副業|投資法|最新投資法|個人向け社債|資産運用).{0,80}(?:レビュー|口コミ|評判|詐欺|稼げ|登録|LINE|ビットコイン|仮想通貨|暗号資産)|"
            r"(?:ドロップ|エンジェルツール).{0,80}(?:副業|投資|口コミ|評判|詐欺|稼げ)|"
            r"(?:ビットコイン|仮想通貨|暗号資産).{0,80}(?:急騰|稼げ|投資|副業|レビュー|口コミ)|"
            r"(?:副業|投資).{0,80}(?:詐欺に遭いやすい|最新レビュー|口コミ評判)"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "loans",
        re.compile(
            r"\b(?:payday\s+loans?|bad\s+credit\s+loans?|cash\s+advance|"
            r"quick\s+loans?|instant\s+loans?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "counterfeit/cheap goods",
        re.compile(
            r"\b(?:(?:cheap|replica|fake|wholesale|discount|outlet|onlinesale|buy)\s+"
            r"(?:jerseys?|air\s*jordans?|oakley|rayban|louis\s*vuitton|gucci|prada|"
            r"nike|adidas|yeezy|sneakers?|handbags?|rolex|michael\s*kors|burberry|"
            r"coach|pandora|tiffany|moncler|north\s*face|canada\s*goose|ugg)|"
            r"(?:nfl|nba|nhl|mlb|soccer|football)\s+jerseys?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "academic writing spam",
        re.compile(
            r"\b(?:buy\s+essay|cheap\s+essay|custom\s+essay|essay\s+writing|"
            r"assignment\s+help|dissertation\s+(?:help|writing)|homework\s+help|"
            r"research\s+paper\s+writing)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "seo/link spam",
        re.compile(
            r"\b(?:buy\s+backlinks?|seo\s+backlinks?|aged\s+domains?|expired\s+domains?|"
            r"pbn\s+(?:links?|network|service)|link\s+building\s+service)\b",
            re.IGNORECASE,
        ),
    ),
)


def custom_spam_words() -> List[str]:
    values: List[str] = []
    raw = os.getenv("WEBARCHIVE_CUSTOM_SPAM_WORDS", "")
    if raw.strip():
        values.extend(re.split(r"[\n,;]+", raw))
    file_name = os.getenv("WEBARCHIVE_CUSTOM_SPAM_FILE", "").strip()
    if file_name:
        try:
            path = Path(file_name)
            values.extend(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            pass
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        word = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(word) < 3:
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(word)
    return result


def scan_script_filters(text: str, locale: str = "") -> List[WebArchiveSpamMatch]:
    return script_matches_from_counts(scan_script_counts(text), locale)


def scan_script_counts(text: str) -> dict[str, int]:
    return {
        category: len(pattern.findall(str(text or "")))
        for category, pattern, _allowed_locales, _sample in SCRIPT_FILTERS
    }


def script_matches_from_counts(counts: dict[str, int], locale: str = "") -> List[WebArchiveSpamMatch]:
    locale_code = str(locale or "").strip().upper()
    matches: List[WebArchiveSpamMatch] = []
    for category, _pattern, allowed_locales, sample in SCRIPT_FILTERS:
        if locale_code in allowed_locales:
            continue
        count = int(counts.get(category, 0) or 0)
        if count >= DEFAULT_SCRIPT_MIN_CHARS:
            matches.append(WebArchiveSpamMatch(category=category, sample=sample, count=count))
    return matches


def webarchive_script_spam_result(
    archive_result: WebArchiveSpamResult,
    locale: str,
) -> WebArchiveSpamResult | None:
    """Apply script mismatch only after the final locale is known by AI."""

    hits: List[WebArchiveSpamHit] = []
    for observation in archive_result.script_observations:
        matches = script_matches_from_counts(observation.counts, locale)
        if matches:
            hits.append(
                WebArchiveSpamHit(
                    timestamp=observation.timestamp,
                    original=observation.original,
                    title=observation.title,
                    matches=matches,
                )
            )
    if not hits:
        return None
    hits.sort(key=lambda hit: hit.timestamp, reverse=True)
    return WebArchiveSpamResult(
        checked=True,
        spam=True,
        snapshots_found=archive_result.snapshots_found,
        snapshots_checked=archive_result.snapshots_checked,
        hits=hits,
        errors=list(archive_result.errors),
        locale_samples=list(archive_result.locale_samples),
        script_observations=list(archive_result.script_observations),
        no_life_found=archive_result.no_life_found,
    )


ARCHIVE_PLACEHOLDER_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "parking",
        re.compile(
            r"(?:this domain (?:name )?is (?:for sale|parked)|buy this domain|domain parking|"
            r"sedo domain parking|afternic|dan\.com/domain|parkingcrew)",
            re.IGNORECASE,
        ),
    ),
    (
        "coming soon",
        re.compile(
            r"(?:website (?:is )?coming soon|site (?:is )?under construction|"
            r"our website is under construction|future home of something quite cool)",
            re.IGNORECASE,
        ),
    ),
    (
        "hosting placeholder",
        re.compile(
            r"(?:apache2 ubuntu default page|welcome to nginx|default web site page|"
            r"web server is functioning normally|account has been suspended|"
            r"hosted by .{0,40}(?:hosting|registrar))",
            re.IGNORECASE,
        ),
    ),
    (
        "error page",
        re.compile(
            r"(?:\b404\s+(?:not found|error)\b|\b502\s+bad gateway\b|"
            r"\bservice unavailable\b|\baccess denied\b)",
            re.IGNORECASE,
        ),
    ),
)

ARCHIVE_LOCALE_SIGNAL_RE = re.compile(
    r"\b(?:about|contact|contacts?|impressum|imprint|legal|privacy|terms|address|office|"
    r"headquarters|company|organisation|organization|developer|publisher|"
    r"country|city|street|phone|telephone|email|e-mail|"
    r"kontakt|adresse|anschrift|unternehmen|geschäft|gesellschaft|"
    r"à propos|mentions légales|adresse|société|contatti|indirizzo|empresa|dirección|"
    r"o nas|kontakt|adres|apie mus|susisiekti|adresas|buveinė|uab|mb)\b",
    re.IGNORECASE,
)

ARCHIVE_IDENTITY_STOPWORDS = {
    "about", "contact", "home", "homepage", "menu", "news", "privacy", "terms",
    "website", "welcome", "copyright", "more", "read", "page", "site", "this",
    "that", "with", "from", "your", "have", "will", "und", "oder", "eine", "einen",
    "der", "die", "das", "den", "von", "pour", "avec", "dans", "della", "delle",
    "para", "como", "przez", "oraz", "jest", "strona",
}

ARCHIVE_GENERIC_TITLES = {
    "home",
    "homepage",
    "home page",
    "index",
    "main page",
    "official site",
    "untitled",
    "welcome",
    "website",
}

ARCHIVE_CONTACT_SIGNAL_PATTERNS: Sequence[re.Pattern[str]] = (
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\w)\+\d[\d\s()./-]{6,}\d"),
    re.compile(
        r"\b(?:uab|gmbh|ag|sarl|s\.a\.?|s\.r\.l\.?|ltd|limited|llc|inc|oy|ab|aps|bv|nv)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:address|street|road|avenue|boulevard|postal|postcode|zip|"
        r"adresse|anschrift|strasse|stra[ßs]e|gasse|weg|platz|"
        r"adresas|gatv[eė]|prospektas|miestas|buvein[eė])\b",
        re.IGNORECASE,
    ),
)


def _has_short_contact_card(title: str, text: str) -> bool:
    clean = re.sub(r"\s+", " ", f"{title} {text}").strip()
    signals = sum(bool(pattern.search(clean)) for pattern in ARCHIVE_CONTACT_SIGNAL_PATTERNS)
    if re.search(r"\b(?:contact|contacts?|kontakt|contatti|contacto|susisiekti)\b", title, re.IGNORECASE):
        signals += 1
    return signals >= 2


def archive_placeholder_reason(title: str, text: str) -> str:
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
    clean = f"{clean_title} {clean_text}".strip()
    for label, pattern in ARCHIVE_PLACEHOLDER_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        # Error phrases and weak hosting/footer wording occur in legitimate
        # articles too. Treat them as placeholders only when they describe the
        # page itself (title/leading short response), not a long site's content.
        title_match = bool(pattern.search(clean_title))
        near_start = match.start() <= len(clean_title) + 320
        if label == "error page" and not (title_match or (len(clean_text) <= 900 and near_start)):
            continue
        if label == "hosting placeholder" and "hosted by" in match.group(0).casefold():
            if not (title_match or (len(clean_text) <= 1200 and near_start)):
                continue
        if label in {"parking", "coming soon"} and not (
            title_match or near_start or len(clean_text) <= 2000
        ):
            continue
        if match:
            return label
    if len(re.sub(r"\s+", "", clean_text)) < 240 and not _has_short_contact_card(clean_title, clean_text):
        return "short placeholder"
    return ""


def _archive_identity_tokens(page: WebArchivePageContent) -> set[str]:
    raw = f"{page.title} {page.text[:1800]}".casefold()
    tokens = [
        token
        for token in re.findall(r"[^\W\d_]{4,}", raw, re.UNICODE)
        if token not in ARCHIVE_IDENTITY_STOPWORDS
    ]
    return {token for token, _count in Counter(tokens).most_common(70)}


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _normalized_archive_title(title: str) -> str:
    return re.sub(r"\W+", " ", str(title or "").casefold()).strip()


def _is_generic_archive_title(title: str) -> bool:
    normalized = _normalized_archive_title(title)
    if not normalized:
        return True
    return normalized in ARCHIVE_GENERIC_TITLES


def _snapshot_month_ordinal(timestamp: str) -> int:
    match = re.match(r"^(\d{4})(\d{2})", str(timestamp or ""))
    if not match:
        return -1
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return -1
    return year * 12 + month - 1


def _locale_excerpt(title: str, text: str, max_chars: int = 1800) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    parts: List[str] = []
    if title:
        parts.append(f"TITLE: {re.sub(r'\s+', ' ', title).strip()[:300]}")
    if clean:
        parts.append(clean[:700])
    for match in ARCHIVE_LOCALE_SIGNAL_RE.finditer(clean):
        start = max(0, match.start() - 220)
        end = min(len(clean), match.end() + 420)
        window = clean[start:end].strip()
        if window and not any(window in part or part in window for part in parts):
            parts.append(window)
        if sum(len(part) for part in parts) >= max_chars:
            break
    return " | ".join(parts)[:max_chars]


def _select_life_cluster(
    pages: Sequence[WebArchivePageContent],
) -> List[WebArchivePageContent]:
    """Return the deterministic stable, non-placeholder era for a site."""

    valid = sorted(
        (
            page
            for page in pages
            if not archive_placeholder_reason(page.title, page.text)
        ),
        key=lambda page: (
            page.snapshot.timestamp,
            page.snapshot.original,
            page.title,
            page.text,
        ),
    )
    if not valid:
        return []

    token_sets = [_archive_identity_tokens(page) for page in valid]
    parents = list(range(len(valid)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(valid)):
        left_title = _normalized_archive_title(valid[left].title)
        left_month = _snapshot_month_ordinal(valid[left].snapshot.timestamp)
        for right in range(left + 1, len(valid)):
            right_title = _normalized_archive_title(valid[right].title)
            right_month = _snapshot_month_ordinal(valid[right].snapshot.timestamp)
            temporally_close = (
                left_month >= 0
                and right_month >= 0
                and abs(right_month - left_month) <= 24
            )
            if not temporally_close:
                continue
            same_title = bool(
                left_title
                and left_title == right_title
                and not _is_generic_archive_title(left_title)
            )
            if same_title or _token_similarity(token_sets[left], token_sets[right]) >= 0.15:
                union(left, right)

    clusters: dict[int, List[WebArchivePageContent]] = {}
    for index, page in enumerate(valid):
        clusters.setdefault(find(index), []).append(page)

    def cluster_order_key(cluster: Sequence[WebArchivePageContent]) -> tuple[Any, ...]:
        ordered = sorted(
            cluster,
            key=lambda page: (page.snapshot.timestamp, page.snapshot.original, page.title, page.text),
        )
        years = {page.snapshot.timestamp[:4] for page in ordered}
        month_values = [
            value
            for value in (_snapshot_month_ordinal(page.snapshot.timestamp) for page in ordered)
            if value >= 0
        ]
        span = max(month_values) - min(month_values) if month_values else 0
        richness = sum(min(len(page.text), 5000) for page in ordered) // max(1, len(ordered))
        earliest = ordered[0].snapshot.timestamp if ordered else "99999999999999"
        fingerprint = tuple(
            (page.snapshot.timestamp, page.snapshot.original, page.title)
            for page in ordered
        )
        # min() makes all tie-breaks explicit and stable. More support, more
        # covered years/span and richer pages win; an exact tie prefers the
        # earlier site era rather than a later takeover.
        return (-len(ordered), -len(years), -span, -richness, earliest, fingerprint)

    return sorted(
        min(clusters.values(), key=cluster_order_key),
        key=lambda page: (page.snapshot.timestamp, page.snapshot.original, page.title, page.text),
    )


def select_locale_samples(
    pages: Sequence[WebArchivePageContent],
    domain: str,
    max_samples: int = 2,
    max_excerpt_chars: int = 1800,
) -> List[WebArchiveLocaleSample]:
    """Pick stable, content-rich snapshots from the actual life of the site."""

    winner = _select_life_cluster(pages)
    if not winner:
        return []

    years = [page.snapshot.timestamp[:4] for page in winner if len(page.snapshot.timestamp) >= 4]
    confidence = "HIGH" if len(winner) >= 3 and len(set(years)) >= 2 else "MEDIUM" if len(winner) >= 2 else "LOW"
    middle_index = (len(winner) - 1) / 2
    domain_brand = re.sub(r"[^a-z0-9]+", " ", str(domain or "").split(".", 1)[0].casefold()).strip()

    def page_score(item: tuple[int, WebArchivePageContent]) -> float:
        index, page = item
        clean = f"{page.title} {page.text}"
        locale_signals = len(ARCHIVE_LOCALE_SIGNAL_RE.findall(clean))
        brand_bonus = 3 if domain_brand and domain_brand in clean.casefold() else 0
        median_penalty = abs(index - middle_index) * 0.8
        return locale_signals * 4 + brand_bonus + min(len(page.text), 5000) / 700 - median_penalty

    ranked = [
        page
        for _index, page in sorted(
            enumerate(winner),
            key=lambda item: (
                -page_score(item),
                item[1].snapshot.timestamp,
                item[1].snapshot.original,
                item[1].title,
            ),
        )
    ]
    selected: List[WebArchivePageContent] = []
    for page in ranked:
        if not selected:
            selected.append(page)
        elif page.snapshot.timestamp[:4] != selected[0].snapshot.timestamp[:4]:
            selected.append(page)
        if len(selected) >= max(1, min(int(max_samples or 1), 3)):
            break

    life_start = years[0] if years else ""
    life_end = years[-1] if years else ""
    return [
        WebArchiveLocaleSample(
            timestamp=page.snapshot.timestamp,
            original=page.snapshot.original,
            title=page.title,
            excerpt=_locale_excerpt(page.title, page.text, max_chars=max_excerpt_chars),
            confidence=confidence,
            life_start=life_start,
            life_end=life_end,
            supporting_snapshots=len(winner),
        )
        for page in selected
        if _locale_excerpt(page.title, page.text, max_chars=max_excerpt_chars)
    ]


def _domain_variants(domain: str) -> List[str]:
    clean = str(domain or "").strip().lower().removeprefix("www.")
    if not clean:
        return []
    return [
        f"http://{clean}/",
        f"https://{clean}/",
        f"http://www.{clean}/",
        f"https://www.{clean}/",
    ]


def _open_text(url: str, timeout: int, max_bytes: int = 1_000_000) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.1",
            "Accept-Language": "en,ru;q=0.8,*;q=0.5",
        },
    )
    opener = build_opener()
    with opener.open(request, timeout=timeout) as response:
        body = response.read(max_bytes)
        charset = response.headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


def _open_text_with_retries(
    url: str,
    timeout: int,
    max_bytes: int = 1_000_000,
    retries: int = 1,
    retry_delay: float = 0.8,
) -> str:
    attempts = max(1, min(int(retries or 0), 3) + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return _open_text(url, timeout=timeout, max_bytes=max_bytes)
        except HTTPError as exc:
            last_exc = exc
            if exc.code in {400, 401, 403, 404, 410} or attempt >= attempts - 1:
                raise
        except (URLError, OSError, TimeoutError, ValueError) as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                raise
        time.sleep(min(3.0, retry_delay * (attempt + 1)))
    if last_exc:
        raise last_exc
    raise URLError("request failed")


def _cdx_query_url(
    original_url: str,
    limit: int,
    from_stamp: str = "",
    to_stamp: str = "",
    sort_reverse: bool = False,
) -> str:
    params: list[tuple[str, str]] = [
        ("url", original_url),
        ("output", "json"),
        ("fl", "timestamp,original,mimetype,statuscode,digest"),
        ("filter", "statuscode:(200|-)"),
        ("filter", "mimetype:(text/html|warc/revisit)"),
        ("collapse", "timestamp:6"),
        ("collapse", "digest"),
        ("limit", str(limit)),
    ]
    if from_stamp:
        params.append(("from", from_stamp))
    if to_stamp:
        params.append(("to", to_stamp))
    if sort_reverse:
        params.append(("sort", "reverse"))
    return WAYBACK_CDX_URL + "?" + urlencode(params)


def _snapshot_from_cdx_row(row: dict[str, Any], fallback_original: str) -> WaybackSnapshot | None:
    timestamp = str(row.get("timestamp") or "")
    if not re.fullmatch(r"\d{14}", timestamp):
        return None
    statuscode = str(row.get("statuscode") or "")
    mimetype = str(row.get("mimetype") or "").lower()
    # Wayback stores many late recrawls as WARC revisit records. They are still
    # viewable snapshots and often mark the actual last life of a dropped domain.
    if statuscode not in {"200", "-"}:
        return None
    if mimetype not in {"text/html", "warc/revisit"}:
        return None
    return WaybackSnapshot(
        timestamp=timestamp,
        original=str(row.get("original") or fallback_original),
        digest=str(row.get("digest") or ""),
        statuscode=statuscode,
        mimetype=mimetype,
    )


def _snapshots_from_cdx_payload(payload: Any, fallback_original: str) -> List[WaybackSnapshot]:
    if not isinstance(payload, list) or len(payload) <= 1:
        return []
    header = [str(value) for value in payload[0]]
    snapshots: List[WaybackSnapshot] = []
    for item in payload[1:]:
        if not isinstance(item, list):
            continue
        snapshot = _snapshot_from_cdx_row(dict(zip(header, item)), fallback_original)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def _load_cdx_snapshots(
    original: str,
    limit: int,
    timeout: int,
    retries: int,
    max_bytes: int,
    from_stamp: str = "",
    to_stamp: str = "",
    sort_reverse: bool = False,
) -> List[WaybackSnapshot]:
    payload = json.loads(
        _open_text_with_retries(
            _cdx_query_url(
                original,
                limit=limit,
                from_stamp=from_stamp,
                to_stamp=to_stamp,
                sort_reverse=sort_reverse,
            ),
            timeout,
            max_bytes,
            retries,
        )
    )
    return _snapshots_from_cdx_payload(payload, original)


def _shift_years(timestamp: str, years: int) -> str:
    """Return a CDX from/to stamp shifted by whole years from a 14-digit timestamp."""

    try:
        value = datetime.strptime(timestamp[:8], "%Y%m%d")
    except ValueError:
        return ""
    target_year = max(1, value.year - max(1, int(years or 1)))
    try:
        shifted = value.replace(year=target_year)
    except ValueError:
        # 29 Feb -> 28 Feb in a non-leap target year.
        shifted = value.replace(year=target_year, day=28)
    return shifted.strftime("%Y%m%d")


def _fetch_latest_snapshots(
    originals: Sequence[str],
    timeout: int,
    retries: int,
) -> tuple[List[tuple[WaybackSnapshot, str]], List[str]]:
    """Find the latest capture independently for every URL variant.

    A bare HTTP variant may have only a recent registrar/parking capture while
    the real historical site lives under HTTPS or ``www``. Stopping at the
    first non-empty variant therefore silently loses the useful site era.
    """

    errors: List[str] = []
    latest_by_variant: List[tuple[WaybackSnapshot, str]] = []
    if not originals:
        return latest_by_variant, errors
    probe_timeout = max(2, min(int(timeout or 8), 5))
    for original in dict.fromkeys(original for original in originals if original):
        try:
            snapshots = _load_cdx_snapshots(
                original,
                limit=1,
                timeout=probe_timeout,
                retries=0,
                max_bytes=150_000,
                sort_reverse=True,
            )
        except (json.JSONDecodeError, HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            errors.append(f"CDX latest {original}: {type(exc).__name__}")
            continue
        except Exception as exc:  # pragma: no cover - defensive network boundary
            errors.append(f"CDX latest {original}: {type(exc).__name__}")
            continue
        if snapshots:
            latest_by_variant.append(
                (
                    max(
                        snapshots,
                        key=lambda snapshot: (snapshot.timestamp, snapshot.original, snapshot.digest),
                    ),
                    original,
                )
            )
    return latest_by_variant, errors


def _fetch_latest_snapshot(
    originals: Sequence[str],
    timeout: int,
    retries: int,
) -> tuple[WaybackSnapshot | None, str, List[str]]:
    """Compatibility wrapper returning the globally latest discovered capture."""

    latest_by_variant, errors = _fetch_latest_snapshots(originals, timeout, retries)
    if not latest_by_variant:
        return None, "", errors
    snapshot, original = max(
        latest_by_variant,
        key=lambda item: (item[0].timestamp, item[1], item[0].digest),
    )
    return snapshot, original, errors


def _bounded_monthly_snapshots(
    records: Sequence[WaybackSnapshot],
    max_snapshots: int,
) -> List[WaybackSnapshot]:
    """Deterministically keep at most one capture per month and bound HTML work."""

    limit = max(1, min(int(max_snapshots or 1), 120))
    by_month: dict[str, WaybackSnapshot] = {}
    seen: set[tuple[str, str, str]] = set()
    for snapshot in sorted(
        records,
        key=lambda value: (value.timestamp, value.original, value.digest),
        reverse=True,
    ):
        key = (snapshot.month, snapshot.digest, snapshot.original)
        if key in seen:
            continue
        seen.add(key)
        by_month.setdefault(snapshot.month, snapshot)
    monthly = [by_month[month] for month in sorted(by_month.keys(), reverse=True)]
    if len(monthly) <= limit:
        return monthly
    if limit == 1:
        return [monthly[0]]
    step = (len(monthly) - 1) / (limit - 1)
    selected: List[WaybackSnapshot] = []
    selected_months: set[str] = set()
    for index in range(limit):
        snapshot = monthly[round(index * step)]
        if snapshot.month not in selected_months:
            selected.append(snapshot)
            selected_months.add(snapshot.month)
    return selected


def _fetch_wayback_window_snapshots(
    originals: Sequence[str],
    from_stamp: str,
    to_stamp: str,
    max_snapshots: int,
    timeout: int,
    retries: int,
) -> tuple[List[WaybackSnapshot], List[str]]:
    """Load one explicit bounded life window without another latest probe."""

    records: List[WaybackSnapshot] = []
    errors: List[str] = []
    per_variant_limit = max(24, max_snapshots * 4)
    for original in dict.fromkeys(original for original in originals if original):
        try:
            snapshots = _load_cdx_snapshots(
                original,
                limit=per_variant_limit,
                timeout=timeout,
                retries=retries,
                max_bytes=750_000,
                from_stamp=from_stamp,
                to_stamp=to_stamp,
            )
        except (json.JSONDecodeError, HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            errors.append(f"CDX policy window {original}: {type(exc).__name__}")
            continue
        except Exception as exc:  # pragma: no cover - defensive network boundary
            errors.append(f"CDX policy window {original}: {type(exc).__name__}")
            continue
        for snapshot in snapshots:
            day_stamp = snapshot.timestamp[:8]
            if from_stamp <= day_stamp <= to_stamp:
                records.append(snapshot)
    return _bounded_monthly_snapshots(records, max_snapshots), errors


def fetch_wayback_snapshots(
    domain: str,
    years: int = 5,
    max_snapshots: int = 24,
    timeout: int = 8,
    retries: int = 1,
    now: datetime | None = None,
) -> tuple[List[WaybackSnapshot], List[str]]:
    now = now or datetime.now(timezone.utc)
    years = max(1, min(int(years or 5), 10))
    # One extra, equally sized window is a bounded discovery guard. A dropped
    # domain often has a late registrar/parking capture on the very same URL
    # variant; looking back only ``years`` from that capture would hide the
    # preceding real site life. This doubles metadata coverage without adding
    # another CDX request, while max_snapshots still caps downloaded pages.
    discovery_years = min(years * 2, 20)
    max_snapshots = max(1, min(int(max_snapshots or 24), 120))
    errors: List[str] = []
    records: List[WaybackSnapshot] = []
    per_variant_limit = max(24, max_snapshots * 4, discovery_years * 12 + 12)

    originals = _domain_variants(domain)
    latest_by_variant, latest_errors = _fetch_latest_snapshots(
        originals,
        timeout=timeout,
        retries=retries,
    )
    errors.extend(latest_errors)
    if not latest_by_variant:
        return [], errors

    # For dropped domains, the window is anchored to the end of archived life,
    # not to today's date. We request the configured window plus one preceding
    # window so a late parking capture does not hide the real prior era. Each
    # URL variant still gets its own independently anchored discovery window.
    for latest_snapshot, original in latest_by_variant:
        from_stamp = _shift_years(latest_snapshot.timestamp, discovery_years)
        to_stamp = latest_snapshot.timestamp[:8]
        if not from_stamp:
            from_stamp = f"{now.year - discovery_years}{now.month:02d}"
        if not to_stamp:
            to_stamp = f"{now.year}{now.month:02d}"
        try:
            snapshots = _load_cdx_snapshots(
                original,
                limit=per_variant_limit,
                timeout=timeout,
                retries=retries,
                max_bytes=750_000,
                from_stamp=from_stamp,
                to_stamp=to_stamp,
            )
        except (json.JSONDecodeError, HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            errors.append(f"CDX {original}: {type(exc).__name__}")
            continue
        except Exception as exc:  # pragma: no cover - defensive network boundary
            errors.append(f"CDX {original}: {type(exc).__name__}")
            continue
        for snapshot in snapshots:
            day_stamp = snapshot.timestamp[:8]
            if from_stamp <= day_stamp <= to_stamp:
                records.append(snapshot)

    return _bounded_monthly_snapshots(records, max_snapshots), errors


def extract_archive_text(html: str, max_chars: int = 8000) -> tuple[str, str]:
    parser = _VisibleArchiveParser()
    parser.feed(html or "")
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()[:300]
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return title, text[:max_chars]


def scan_wayback_text(
    text: str,
    locale: str = "",
    include_scripts: bool = True,
) -> List[WebArchiveSpamMatch]:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return []
    matches: List[WebArchiveSpamMatch] = []
    if include_scripts:
        matches.extend(scan_script_filters(clean, locale=locale))
    for category, pattern in SPAM_PATTERNS:
        found = [match.group(0).strip() for match in pattern.finditer(clean)]
        if found:
            sample = found[0]
            matches.append(WebArchiveSpamMatch(category=category, sample=sample, count=len(found)))
    custom_found = [
        word
        for word in custom_spam_words()
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", clean, re.IGNORECASE)
    ]
    if custom_found:
        matches.append(
            WebArchiveSpamMatch(
                category="custom spam words",
                sample=custom_found[0],
                count=len(custom_found),
            )
        )
    return matches


def fetch_wayback_snapshot_text(
    snapshot: WaybackSnapshot,
    timeout: int = 8,
    max_chars: int = 8000,
    retries: int = 1,
) -> tuple[WaybackSnapshot, str, str, str]:
    url = WAYBACK_WEB_URL.format(timestamp=snapshot.timestamp, original=snapshot.original)
    try:
        html = _open_text_with_retries(
            url,
            timeout=timeout,
            max_bytes=max(100_000, max_chars * 12),
            retries=retries,
        )
        title, text = extract_archive_text(html, max_chars=max_chars)
        return snapshot, title, text, ""
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return snapshot, "", "", f"{snapshot.display_month}: {type(exc).__name__}"


def check_webarchive_spam(
    domain: str,
    locale: str = "",
    years: int = 5,
    max_snapshots: int = 24,
    timeout: int = 8,
    max_chars: int = 8000,
    max_workers: int = 4,
    retries: int = 1,
    scan_scripts: bool = True,
) -> WebArchiveSpamResult:
    policy_years = max(1, min(int(years or 5), 10))
    html_budget = max(1, min(int(max_snapshots or 24), 120))
    discovery_years = min(policy_years * 2, 20)
    snapshots, errors = fetch_wayback_snapshots(
        domain,
        years=policy_years,
        max_snapshots=html_budget,
        timeout=timeout,
        retries=retries,
    )
    if not snapshots:
        return WebArchiveSpamResult(
            checked=False,
            spam=False,
            snapshots_found=0,
            snapshots_checked=0,
            errors=errors,
        )

    snapshot_key = lambda snapshot: (snapshot.timestamp, snapshot.original)
    attempted: set[tuple[str, str]] = set()
    loaded: dict[tuple[str, str], WebArchivePageContent] = {}
    fetch_error_by_key: dict[tuple[str, str], str] = {}

    def fetch_batch(candidates: Sequence[WaybackSnapshot], limit: int) -> None:
        remaining = max(0, html_budget - len(attempted))
        wanted = [
            snapshot
            for snapshot in candidates
            if snapshot_key(snapshot) not in attempted
        ][: min(max(0, limit), remaining)]
        if not wanted:
            return
        for snapshot in wanted:
            attempted.add(snapshot_key(snapshot))
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(wanted)))) as pool:
            futures = [
                pool.submit(
                    fetch_wayback_snapshot_text,
                    snapshot,
                    timeout,
                    max_chars,
                    retries,
                )
                for snapshot in wanted
            ]
            for future in as_completed(futures):
                snapshot, title, text, error = future.result()
                key = snapshot_key(snapshot)
                if error:
                    fetch_error_by_key[key] = error
                    continue
                loaded[key] = WebArchivePageContent(snapshot=snapshot, title=title, text=text)

    # Spend only half of the HTML budget on broad discovery. The other half is
    # reserved for the exact configured life window if late parking shifted the
    # initial CDX anchor away from the real site era.
    discovery_budget = min(len(snapshots), max(1, (html_budget + 1) // 2))
    discovery_candidates = _bounded_monthly_snapshots(snapshots, discovery_budget)
    fetch_batch(discovery_candidates, discovery_budget)
    discovery_pages = [
        loaded[snapshot_key(snapshot)]
        for snapshot in discovery_candidates
        if snapshot_key(snapshot) in loaded
    ]
    life_cluster = _select_life_cluster(discovery_pages)
    if not life_cluster:
        discovery_fetch_errors = [
            fetch_error_by_key[snapshot_key(snapshot)]
            for snapshot in discovery_candidates
            if snapshot_key(snapshot) in fetch_error_by_key
        ]
        return WebArchiveSpamResult(
            checked=False,
            spam=False,
            snapshots_found=len(snapshots),
            snapshots_checked=0,
            errors=errors + discovery_fetch_errors,
            no_life_found=not discovery_fetch_errors,
        )

    effective_end = max(page.snapshot.timestamp for page in life_cluster)
    # A failed capture newer than the chosen cluster leaves the real end of
    # site life unresolved. Do not anchor an apparently clean policy window to
    # an older successful page while newer candidate pages timed out.
    anchor_errors = [
        fetch_error_by_key[snapshot_key(snapshot)]
        for snapshot in discovery_candidates
        if snapshot.timestamp > effective_end
        and snapshot_key(snapshot) in fetch_error_by_key
    ]
    policy_from = _shift_years(effective_end, policy_years)
    policy_to = effective_end[:8]
    discovery_end = max(snapshot.timestamp for snapshot in snapshots)
    discovery_from = _shift_years(discovery_end, discovery_years)

    window_errors: List[str] = []
    policy_metadata = [
        snapshot
        for snapshot in snapshots
        if policy_from <= snapshot.timestamp[:8] <= policy_to
    ]
    if policy_from and discovery_from and policy_from < discovery_from:
        earlier_metadata, window_errors = _fetch_wayback_window_snapshots(
            _domain_variants(domain),
            from_stamp=policy_from,
            to_stamp=policy_to,
            max_snapshots=html_budget,
            timeout=timeout,
            retries=retries,
        )
        policy_metadata.extend(earlier_metadata)

    policy_snapshots = _bounded_monthly_snapshots(policy_metadata, html_budget)
    remaining_budget = max(0, html_budget - len(attempted))
    unfetched_policy = [
        snapshot
        for snapshot in policy_snapshots
        if snapshot_key(snapshot) not in attempted
    ]
    fetch_batch(
        _bounded_monthly_snapshots(unfetched_policy, max(1, remaining_budget))
        if remaining_budget and unfetched_policy
        else [],
        remaining_budget,
    )

    policy_keys = {snapshot_key(snapshot) for snapshot in policy_snapshots}
    policy_pages = [
        page
        for key, page in loaded.items()
        if key in policy_keys
    ]
    meaningful_pages = [
        page
        for page in policy_pages
        if (page.title.strip() or page.text.strip())
        and not archive_placeholder_reason(page.title, page.text)
    ]

    hits: List[WebArchiveSpamHit] = []
    script_observations: List[WebArchiveScriptObservation] = []
    for page in meaningful_pages:
        combined_text = f"{page.title} {page.text}"
        script_observations.append(
            WebArchiveScriptObservation(
                timestamp=page.snapshot.timestamp,
                original=page.snapshot.original,
                title=page.title,
                counts=scan_script_counts(combined_text),
            )
        )
        matches = scan_wayback_text(
            combined_text,
            locale=locale,
            include_scripts=scan_scripts,
        )
        if matches:
            hits.append(
                WebArchiveSpamHit(
                    timestamp=page.snapshot.timestamp,
                    original=page.snapshot.original,
                    title=page.title,
                    matches=matches,
                )
            )

    hits.sort(key=lambda hit: hit.timestamp, reverse=True)
    locale_samples = select_locale_samples(meaningful_pages, domain=domain)
    checked = len(meaningful_pages)
    required_checked = 1 if len(policy_snapshots) == 1 else max(2, (len(policy_snapshots) + 1) // 2)
    coverage_ok = (
        bool(policy_snapshots)
        and checked >= required_checked
        and not anchor_errors
        and not window_errors
    )
    policy_fetch_errors = [
        fetch_error_by_key[key]
        for key in policy_keys
        if key in fetch_error_by_key
    ]
    relevant_fetch_errors = list(dict.fromkeys(anchor_errors + policy_fetch_errors))
    no_life_found = not hits and not coverage_ok and not (window_errors or relevant_fetch_errors)
    coverage_errors: List[str] = []
    if not hits and not coverage_ok and not no_life_found:
        coverage_errors.append(
            f"incomplete usable snapshot coverage: {checked}/{len(policy_snapshots)}"
        )
    return WebArchiveSpamResult(
        # A positive spam hit is conclusive even if other captures failed. A
        # clean verdict needs representative non-placeholder coverage inside
        # the configured window; discovery-only pages cannot decide the gate.
        checked=bool(hits) or coverage_ok,
        spam=bool(hits),
        snapshots_found=len(policy_snapshots),
        snapshots_checked=checked,
        hits=hits,
        errors=errors + window_errors + relevant_fetch_errors + coverage_errors,
        locale_samples=locale_samples,
        script_observations=script_observations,
        no_life_found=no_life_found,
    )
