"""Small Wayback/WebArchive spam gate used before accepting GOOD domains.

The checker intentionally stays deterministic: it uses Wayback CDX metadata,
downloads a bounded number of archived HTML snapshots, extracts visible text,
and scans it with high-confidence spam-topic regexes.
"""

from __future__ import annotations

import json
import re
import time
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
class WebArchiveSpamResult:
    checked: bool
    spam: bool
    snapshots_found: int = 0
    snapshots_checked: int = 0
    hits: List[WebArchiveSpamHit] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

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
    locale_code = str(locale or "").strip().upper()
    matches: List[WebArchiveSpamMatch] = []
    for category, pattern, allowed_locales, sample in SCRIPT_FILTERS:
        if locale_code in allowed_locales:
            continue
        count = len(pattern.findall(text))
        if count >= DEFAULT_SCRIPT_MIN_CHARS:
            matches.append(WebArchiveSpamMatch(category=category, sample=sample, count=count))
    return matches


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


def _fetch_latest_snapshot(
    originals: Sequence[str],
    timeout: int,
    retries: int,
) -> tuple[WaybackSnapshot | None, str, List[str]]:
    """Find the first working latest snapshot; this defines the domain-life window."""

    errors: List[str] = []
    if not originals:
        return None, "", errors
    probe_timeout = max(2, min(int(timeout or 8), 5))
    for original in originals:
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
            return max(snapshots, key=lambda snapshot: snapshot.timestamp), original, errors
    return None, "", errors


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
    max_snapshots = max(1, min(int(max_snapshots or 24), 120))
    errors: List[str] = []
    records: List[WaybackSnapshot] = []
    per_variant_limit = max(12, max_snapshots * 4)

    originals = _domain_variants(domain)
    latest_snapshot, latest_original, latest_errors = _fetch_latest_snapshot(originals, timeout=timeout, retries=retries)
    errors.extend(latest_errors)
    if latest_snapshot is None:
        return [], errors

    # For dropped domains, "last N years" means the last N years of the domain's
    # archived life, not the last N years from today. Example: if the last
    # capture is in 2018, a 5-year gate should inspect 2013-2018.
    from_stamp = _shift_years(latest_snapshot.timestamp, years)
    to_stamp = latest_snapshot.timestamp[:8]
    if not from_stamp:
        from_stamp = f"{now.year - years}{now.month:02d}"
    if not to_stamp:
        to_stamp = f"{now.year}{now.month:02d}"

    range_originals = [latest_original] if latest_original else []
    range_originals.extend(original for original in originals if original and original != latest_original)
    for original in range_originals:
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
        if records:
            break

    by_month: dict[str, WaybackSnapshot] = {}
    seen: set[tuple[str, str, str]] = set()
    for snapshot in sorted(records, key=lambda value: value.timestamp, reverse=True):
        key = (snapshot.month, snapshot.digest, snapshot.original)
        if key in seen:
            continue
        seen.add(key)
        by_month.setdefault(snapshot.month, snapshot)
    monthly = [by_month[month] for month in sorted(by_month.keys(), reverse=True)]
    if len(monthly) <= max_snapshots:
        return monthly, errors

    if max_snapshots == 1:
        return [monthly[0]], errors
    step = (len(monthly) - 1) / (max_snapshots - 1)
    selected: List[WaybackSnapshot] = []
    selected_months: set[str] = set()
    for index in range(max_snapshots):
        snapshot = monthly[round(index * step)]
        if snapshot.month not in selected_months:
            selected.append(snapshot)
            selected_months.add(snapshot.month)
    return selected, errors


def extract_archive_text(html: str, max_chars: int = 8000) -> tuple[str, str]:
    parser = _VisibleArchiveParser()
    parser.feed(html or "")
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()[:300]
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return title, text[:max_chars]


def scan_wayback_text(text: str, locale: str = "") -> List[WebArchiveSpamMatch]:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return []
    matches: List[WebArchiveSpamMatch] = []
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
) -> WebArchiveSpamResult:
    snapshots, errors = fetch_wayback_snapshots(
        domain,
        years=years,
        max_snapshots=max_snapshots,
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

    hits: List[WebArchiveSpamHit] = []
    fetch_errors: List[str] = []
    checked = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(snapshots)))) as pool:
        futures = [
            pool.submit(
                fetch_wayback_snapshot_text,
                snapshot,
                timeout,
                max_chars,
                retries,
            )
            for snapshot in snapshots
        ]
        for future in as_completed(futures):
            snapshot, title, text, error = future.result()
            if error:
                fetch_errors.append(error)
                continue
            checked += 1
            matches = scan_wayback_text(f"{title} {text}", locale=locale)
            if matches:
                hits.append(
                    WebArchiveSpamHit(
                        timestamp=snapshot.timestamp,
                        original=snapshot.original,
                        title=title,
                        matches=matches,
                    )
                )

    hits.sort(key=lambda hit: hit.timestamp, reverse=True)
    return WebArchiveSpamResult(
        checked=checked > 0,
        spam=bool(hits),
        snapshots_found=len(snapshots),
        snapshots_checked=checked,
        hits=hits,
        errors=errors + fetch_errors,
    )
