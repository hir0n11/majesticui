import csv
import atexit
import html
import io
import json
import logging
import os
import queue as thread_queue
import random
import re
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from domain_ai import (
    DomainVerdict,
    OpenAIDomainChecker,
    clean_freshness_cutoff_year,
    clean_freshness_old_share_percent,
    local_domain_name_precheck,
)
from majestic_reports import (
    MajesticLoginRequired,
    MajesticReportError,
    collect_anchor_text,
    collect_backlinks,
    collect_pages,
)
from webarchive_spam import check_webarchive_spam

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except Exception:  # pragma: no cover
    gspread = None
    ServiceAccountCredentials = None

# =========================
# Конфиг
# =========================
HOST = "127.0.0.1"
PORT = 5000

MAJESTIC_HOME_URL = "https://majestic.com/"
CTX_URL = (
    "https://majestic.com/reports/site-explorer/link-context"
    "?q={q}&scope=domain&IndexDataSource=F&MaxSourceUrlsPerRefDomain=1&removeDeleted=0"
)

CHROME_USER_DATA_DIR = r"C:\MajesticSeleniumProfile"
CHROME_PROFILE_DIRECTORY = "Default"
HEADLESS = False

GOOGLE_SHEETS_ENABLED = True
GOOGLE_SHEET_ID = "1P00cm2QvYQx5kho9i4cH8G02E6eSiMyg0qO7i7vtBX4"
GOOGLE_SHEET_NAME = "Majestic Local UI"
GOOGLE_ARCHIVE_SHEET_NAME = "Archive"
GOOGLE_CREDS_PATH = "credentials.json"

APP_DIR = Path(__file__).resolve().parent
DUPLICATES_FILE = APP_DIR / "duplicates_drops.txt"
RESULTS_CSV_FILE = APP_DIR / "results.csv"
BACKLINKS_DEBUG_FILE = APP_DIR / "last_backlinks_debug.json"
PID_FILE = APP_DIR / "majui.pid"
PROMPT_FILES = {
    "screen_prompt": APP_DIR / "domain_screen_prompt.txt",
    "link_prompt": APP_DIR / "domain_drop_prompt.txt",
    "article_prompt": APP_DIR / "domain_article_prompt.txt",
    "anchor_prompt": APP_DIR / "domain_anchor_prompt.txt",
}
PROMPT_MAX_CHARS = 120_000
PROMPT_FILE_LOCK = threading.RLock()


def env_int(name: str, default: int, minimum: int = 1, maximum: Optional[int] = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


MAJESTIC_MAX_BACKLINKS = env_int("MAJESTIC_MAX_BACKLINKS", 200, 1, 5000)
MAJESTIC_MAX_ANCHORS = env_int("MAJESTIC_MAX_ANCHORS", 500, 1, 5000)
MAJESTIC_MAX_HISTORIC_PAGES = env_int("MAJESTIC_MAX_HISTORIC_PAGES", 50, 1, 500)
WEBARCHIVE_SPAM_ENABLED = bool(env_int("WEBARCHIVE_SPAM_ENABLED", 1, 0, 1))
WEBARCHIVE_SPAM_YEARS = env_int("WEBARCHIVE_SPAM_YEARS", 5, 1, 10)
WEBARCHIVE_SPAM_MAX_SNAPSHOTS = env_int("WEBARCHIVE_SPAM_MAX_SNAPSHOTS", 24, 1, 120)
WEBARCHIVE_SPAM_TIMEOUT = env_int("WEBARCHIVE_SPAM_TIMEOUT", 8, 2, 30)
WEBARCHIVE_SPAM_RETRIES = env_int("WEBARCHIVE_SPAM_RETRIES", 1, 0, 3)
WEBARCHIVE_SPAM_MAX_CHARS = env_int("WEBARCHIVE_SPAM_MAX_CHARS", 8000, 1000, 30000)
WEBARCHIVE_TIMEOUT_RETRY_DELAY = env_int("WEBARCHIVE_TIMEOUT_RETRY_DELAY", 20, 1, 3600)
WEBARCHIVE_RETRY_MAX_DELAY = env_int("WEBARCHIVE_RETRY_MAX_DELAY", 5, 1, 3600)
WEBARCHIVE_WORKERS = env_int("WEBARCHIVE_WORKERS", 8, 1, 32)
PENDING_WEBARCHIVE_STATUS = "PENDING:WEBARCHIVE"
PENDING_AI_STATUS = "PENDING:AI"
OPENAI_AI_WORKERS = env_int("OPENAI_AI_WORKERS", 2, 1, 4)
MAJESTIC_DOMAIN_DELAY_MIN = env_int("MAJESTIC_DOMAIN_DELAY_MIN", 5, 0, 300)
MAJESTIC_DOMAIN_DELAY_MAX = env_int("MAJESTIC_DOMAIN_DELAY_MAX", 14, 0, 600)
MAJESTIC_LONG_PAUSE_EVERY = env_int("MAJESTIC_LONG_PAUSE_EVERY", 40, 0, 5000)
MAJESTIC_LONG_PAUSE_MIN = env_int("MAJESTIC_LONG_PAUSE_MIN", 60, 0, 3600)
MAJESTIC_LONG_PAUSE_MAX = env_int("MAJESTIC_LONG_PAUSE_MAX", 180, 0, 7200)
RESULTS_FILE_LOCK = threading.RLock()

OUTBOUND_LIMIT_HARD = 300
OUTBOUND_LIMIT_SOFT = 120
LD_LISTING_GATE = 75
THRESHOLD_EXT_BIG = 0.75
ROOTISH_RATIO_MIN = 0.35
LD_THRESHOLD_BAD = 70
LD_SHARE_BAD = 0.70
MIN_ROWS = 6
MAX_RETRIES_PER_DOMAIN = 4
MAX_BROWSER_RECOVERIES_PER_DOMAIN = 1
MAX_REPORT_RETRIES = 3
AI_ELIGIBLE_MAJESTIC_STATUSES = {"GOOD", "GOOD OLD", "REVIEW:L"}
FINAL_GOOD_STATUSES = {"GOOD", "GOOD (NEAR THRESHOLD)"}
QUALITY_MODEL_CHOICES = ("gpt-5.6-sol", "gpt-5.6-terra")
NON_LATIN_ARCHIVE_LOCALES = {"CN", "JP", "KR", "TW", "HK", "MO"}
PAGE_LOAD_TIMEOUT = 60
ARTICLE_BROWSER_PAGE_LOAD_TIMEOUT = env_int("ARTICLE_BROWSER_PAGE_LOAD_TIMEOUT", 15, 5, 60)
YEAR_RE = re.compile(r"\b20\d{2}\b")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.I,
)


def clean_quality_model(value: Any, fallback: str = "gpt-5.6-sol") -> str:
    candidate = str(value or "").strip().lower()
    fallback_value = str(fallback or "gpt-5.6-sol").strip().lower()
    if fallback_value not in QUALITY_MODEL_CHOICES:
        fallback_value = "gpt-5.6-sol"
    return candidate if candidate in QUALITY_MODEL_CHOICES else fallback_value

LOCALE_SEGMENTS = {
    "en", "de", "fr", "es", "it", "pt", "pl", "nl", "sv", "no", "da", "fi",
    "cs", "sk", "sl", "hr", "hu", "ro", "bg", "el", "lt", "lv", "et", "ru",
    "uk", "tr", "ar", "ja", "ko", "zh"
}

app = Flask(__name__)


def normalize_domain(value: Any) -> str:
    """Normalize a pasted URL/domain to one bare hostname everywhere."""

    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw) else f"http://{raw}"
    try:
        host = (urlparse(candidate).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def read_active_prompts() -> Dict[str, Any]:
    with PROMPT_FILE_LOCK:
        values: Dict[str, Any] = {}
        modified = 0.0
        for name, path in PROMPT_FILES.items():
            values[name] = path.read_text(encoding="utf-8")
            modified = max(modified, path.stat().st_mtime)
        values["updated_at"] = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S") if modified else ""
        return values


def save_active_prompts(payload: Dict[str, Any]) -> Dict[str, Any]:
    with PROMPT_FILE_LOCK:
        prepared: Dict[str, str] = {}
        for name in PROMPT_FILES:
            value = str(payload.get(name, "")).strip()
            if not value:
                raise ValueError(f"Промпт {name} не может быть пустым")
            if len(value) > PROMPT_MAX_CHARS:
                raise ValueError(f"Промпт {name} превышает {PROMPT_MAX_CHARS} символов")
            prepared[name] = value + "\n"
        temp_paths: Dict[str, Path] = {}
        for name, value in prepared.items():
            path = PROMPT_FILES[name]
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(value, encoding="utf-8")
            temp_paths[name] = temp_path
        for name, temp_path in temp_paths.items():
            os.replace(temp_path, PROMPT_FILES[name])
        return read_active_prompts()


@dataclass
class QueueItem:
    item_id: int
    batch_id: int
    title: str
    domain: str
    state: str = "queued"  # queued | processing | done | removed


@dataclass
class ResultRow:
    title: str
    domain: str
    status: str
    error: str = ""
    majestic_status: str = ""
    ai_verdict: str = ""
    ai_reason: str = ""
    locale: str = ""
    locale_source: str = ""
    unique_quality: int = 0
    article_links: float = 0
    homepage_links: int = 0
    link_year_min: int = 0
    link_year_max: int = 0
    link_year_count: int = 0
    anchor_risk: str = ""
    ai_model: str = ""
    ai_input_tokens: int = 0
    ai_output_tokens: int = 0
    ai_api_calls: int = 0
    ai_backlinks_sent: int = 0
    ai_anchors_sent: int = 0
    ai_early_stop_stage: str = ""
    ai_status: str = ""
    webarchive_status: str = ""
    finished_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@dataclass
class AITask:
    row: ResultRow
    majestic_status: str
    backlinks_report: Dict[str, Any]
    historic_pages: Dict[str, Any]
    fresh_anchors: Dict[str, Any]
    historic_anchors: Dict[str, Any]
    settings: Dict[str, Any]


@dataclass
class WebArchiveTask:
    row: ResultRow
    original_status: str
    original_reason: str
    attempts: int = 0


def format_link_year_range(row: ResultRow) -> str:
    try:
        year_min = int(row.link_year_min or 0)
        year_max = int(row.link_year_max or 0)
        year_count = int(row.link_year_count or 0)
        unique_quality = int(row.unique_quality or 0)
    except (TypeError, ValueError):
        return ""
    if year_min <= 0 or year_max <= 0:
        return ""
    label = f"Y{year_min}" if year_min == year_max else f"Y{year_min}-{year_max}"
    if year_count > 0 and unique_quality > 0 and year_count < unique_quality:
        label = f"{label} {year_count}/{unique_quality}"
    return label


def format_compact_metric(row: ResultRow) -> str:
    try:
        article_value = float(row.article_links or 0)
    except (TypeError, ValueError):
        article_value = 0.0
    articles = "AI" if article_value < 0 else (
        str(int(article_value)) if article_value.is_integer() else f"{article_value:.1f}".rstrip("0").rstrip(".")
    )
    parts = [f"U{int(row.unique_quality or 0)}", f"A{articles}", f"H{int(row.homepage_links or 0)}"]
    year_range = format_link_year_range(row)
    if year_range:
        parts.append(year_range)
    return " ".join(parts)


def should_archive_locale(locale: Any) -> bool:
    return str(locale or "").strip().upper() in NON_LATIN_ARCHIVE_LOCALES


class UILogHandler(logging.Handler):
    def __init__(self, state: "AppState") -> None:
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        self.state.append_log(self.format(record))


class DuplicateStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.lock = threading.RLock()
        self.domains = self._load()

    def _load(self) -> set[str]:
        if not self.file_path.exists():
            return set()
        try:
            return {
                normalized
                for line in self.file_path.read_text(encoding="utf-8").splitlines()
                if (normalized := normalize_domain(line)) and DOMAIN_RE.fullmatch(normalized)
            }
        except Exception:
            return set()

    def __contains__(self, domain: str) -> bool:
        normalized = normalize_domain(domain)
        with self.lock:
            return normalized in self.domains

    def add(self, domain: str) -> bool:
        normalized = normalize_domain(domain)
        if not normalized:
            return False
        with self.lock:
            if normalized in self.domains:
                return False
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(normalized + "\n")
            self.domains.add(normalized)
            return True

    @staticmethod
    def normalize_many(raw_domains: str) -> set[str]:
        result: set[str] = set()
        for line in str(raw_domains or "").splitlines():
            value = normalize_domain(line)
            if DOMAIN_RE.fullmatch(value):
                result.add(value)
        return result

    def remove_many(self, raw_domains: str) -> Dict[str, int]:
        requested = self.normalize_many(raw_domains)
        if not requested:
            raise ValueError("Не найдено ни одного корректного домена")
        with self.lock:
            found = requested & self.domains
            self.domains.difference_update(found)
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            temp_path.write_text(
                "".join(f"{domain}\n" for domain in sorted(self.domains)),
                encoding="utf-8",
            )
            os.replace(temp_path, self.file_path)
        return {
            "requested": len(requested),
            "removed": len(found),
            "not_found": len(requested - found),
            "remaining": len(self.domains),
        }

    def size(self) -> int:
        with self.lock:
            return len(self.domains)


class GoogleSheetsSink:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.worksheet = None
        self.archive_worksheet = None
        self.enabled = GOOGLE_SHEETS_ENABLED
        self.ready = False
        self.last_error = ""

    def init(self) -> None:
        with self.lock:
            if not self.enabled:
                self.ready = False
                self.last_error = "Google Sheets disabled by config"
                return
            if self.ready and self.worksheet is not None:
                return
            if gspread is None or ServiceAccountCredentials is None:
                self.ready = False
                self.last_error = "gspread/oauth2client не установлены"
                return
            try:
                scope = [
                    "https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/spreadsheets",
                ]
                creds_path = Path(GOOGLE_CREDS_PATH)
                if not creds_path.is_absolute():
                    creds_path = APP_DIR / creds_path
                creds = ServiceAccountCredentials.from_json_keyfile_name(str(creds_path), scope)
                client = gspread.authorize(creds)
                spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
                self.worksheet = spreadsheet.worksheet(GOOGLE_SHEET_NAME)
                try:
                    self.archive_worksheet = spreadsheet.worksheet(GOOGLE_ARCHIVE_SHEET_NAME)
                except Exception:
                    self.archive_worksheet = spreadsheet.add_worksheet(
                        title=GOOGLE_ARCHIVE_SHEET_NAME,
                        rows=1000,
                        cols=10,
                    )
                self.ready = True
                self.last_error = ""
                logger.info("Google Sheets enabled")
            except Exception as e:
                self.worksheet = None
                self.archive_worksheet = None
                self.ready = False
                self.last_error = f"{type(e).__name__}: {e}"
                logger.error(f"Google Sheets disabled: {self.last_error}")

    def append_good(self, row: ResultRow) -> bool:
        with self.lock:
            if not self.enabled:
                return False
            if not self.ready or self.worksheet is None:
                self.init()
            if not self.ready or self.worksheet is None:
                return False
            target_worksheet = self.archive_worksheet if should_archive_locale(row.locale) else self.worksheet
            if target_worksheet is None:
                return False
            try:
                target_worksheet.append_row(
                    [
                        row.title,
                        row.domain,
                        row.status,
                        f"{row.locale} · {row.locale_source}".strip(" ·"),
                        format_compact_metric(row),
                        row.ai_reason,
                    ],
                    value_input_option="USER_ENTERED",
                )
                return True
            except Exception as e:
                self.ready = False
                self.last_error = f"{type(e).__name__}: {e}"
                logger.error(f"Sheets append failed: {self.last_error}")
                return False


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.queue: List[QueueItem] = []
        self.results: List[ResultRow] = []
        self.logs: List[str] = []
        self.current_domain = ""
        self.current_title = ""
        self.current_item_id: Optional[int] = None
        self.browser_ready = False
        self.browser_launching = False
        self.browser_error = ""
        self.login_required = False
        self.running = False
        self.paused = False
        self.stop_requested = False
        self.worker_alive = False
        self.has_started_once = False
        self.browser_recovery_in_progress = False
        self.last_status = "IDLE"
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.login_event = threading.Event()
        self.processing_thread: Optional[threading.Thread] = None
        self.ai_threads: List[threading.Thread] = []
        self.webarchive_threads: List[threading.Thread] = []
        self.next_item_id = 1
        self.next_batch_id = 1
        self.batch_order: List[int] = []
        self.batch_title_map: Dict[str, int] = {}
        self.load_stats = {
            "loaded": 0,
            "duplicates_skipped": 0,
            "duplicates_from_file": 0,
            "titles": 0,
            "invalid_skipped": 0,
        }
        self.strict_mode = bool(env_int("OPENAI_STRICT_NEAR_MODE", 0, 0, 1))
        self.strict_unique_deficit = env_int("OPENAI_STRICT_NEAR_UNIQUE_DEFICIT", 1, 0, 50)
        self.strict_article_deficit = env_int("OPENAI_STRICT_NEAR_ARTICLE_DEFICIT", 1, 0, 50)
        self.freshness_filter_enabled = bool(env_int("OPENAI_FRESHNESS_FILTER_ENABLED", 1, 0, 1))
        self.freshness_cutoff_year = clean_freshness_cutoff_year(
            env_int("OPENAI_FRESHNESS_CUTOFF_YEAR", 2016, 1990, 2030)
        )
        self.freshness_max_old_share_percent = clean_freshness_old_share_percent(
            env_int("OPENAI_FRESHNESS_MAX_OLD_SHARE", 50, 0, 100)
        )
        self.quality_model = clean_quality_model(os.getenv("OPENAI_DOMAIN_MODEL", "gpt-5.6-sol"))

    def append_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-500:]

    def clear_logs(self) -> None:
        with self.lock:
            self.logs.clear()

    def set_strict_settings(
        self,
        enabled: bool,
        unique_deficit: Optional[Any] = None,
        article_deficit: Optional[Any] = None,
        quality_model: Optional[Any] = None,
        freshness_filter_enabled: Optional[Any] = None,
        freshness_cutoff_year: Optional[Any] = None,
        freshness_max_old_share_percent: Optional[Any] = None,
    ) -> Dict[str, Any]:
        def clean(value: Optional[Any], fallback: int) -> int:
            if value is None:
                return fallback
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = fallback
            return max(0, min(parsed, 50))

        with self.lock:
            self.strict_mode = bool(enabled)
            self.strict_unique_deficit = clean(unique_deficit, self.strict_unique_deficit)
            self.strict_article_deficit = clean(article_deficit, self.strict_article_deficit)
            self.quality_model = clean_quality_model(quality_model, self.quality_model)
            if freshness_filter_enabled is not None:
                self.freshness_filter_enabled = bool(freshness_filter_enabled)
            self.freshness_cutoff_year = clean_freshness_cutoff_year(
                freshness_cutoff_year,
                self.freshness_cutoff_year,
            )
            self.freshness_max_old_share_percent = clean_freshness_old_share_percent(
                freshness_max_old_share_percent,
                self.freshness_max_old_share_percent,
            )
            return {
                "strict_mode": self.strict_mode,
                "strict_unique_deficit": self.strict_unique_deficit,
                "strict_article_deficit": self.strict_article_deficit,
                "quality_model": self.quality_model,
                "freshness_filter_enabled": self.freshness_filter_enabled,
                "freshness_cutoff_year": self.freshness_cutoff_year,
                "freshness_max_old_share_percent": self.freshness_max_old_share_percent,
            }

    def _normalize_domain(self, domain: str) -> str:
        return normalize_domain(domain)

    def _existing_active_domains(self) -> set[str]:
        return {item.domain for item in self.queue if item.state in {"queued", "processing"}}

    def add_batch(self, title: str, raw_domains: str, duplicate_store: DuplicateStore) -> Dict[str, Any]:
        title = (title or "").strip() or "NO TITLE"
        title_key = title.casefold()
        local_seen = set()
        domains_to_add: List[str] = []
        skipped_local = 0
        skipped_duplicate_file = 0
        skipped_invalid = 0

        with self.lock:
            existing = self._existing_active_domains()
            batch_id = self.batch_title_map.get(title_key)
            created_new_batch = batch_id is None

        for line in raw_domains.splitlines():
            domain = self._normalize_domain(line)
            if not domain:
                continue
            if not DOMAIN_RE.match(domain):
                skipped_invalid += 1
                continue
            if domain in local_seen or domain in existing:
                skipped_local += 1
                continue
            if domain in duplicate_store:
                skipped_duplicate_file += 1
                continue
            local_seen.add(domain)
            domains_to_add.append(domain)

        items: List[QueueItem] = []
        with self.lock:
            current_batch_id = self.batch_title_map.get(title_key)
            if current_batch_id is not None:
                batch_id = current_batch_id
                created_new_batch = False
            elif domains_to_add:
                batch_id = self.next_batch_id
                self.next_batch_id += 1
                self.batch_order.append(batch_id)
                self.batch_title_map[title_key] = batch_id
                created_new_batch = True
            else:
                batch_id = None
            # Another concurrent add request may have changed the queue while
            # duplicate-file checks were running.
            existing_now = self._existing_active_domains()
            for domain in domains_to_add:
                if domain in existing_now:
                    skipped_local += 1
                    continue
                items.append(
                    QueueItem(
                        item_id=self.next_item_id,
                        batch_id=batch_id,
                        title=title,
                        domain=domain,
                    )
                )
                self.next_item_id += 1
                existing_now.add(domain)

            if items:
                self.queue.extend(items)
                self.last_status = "READY" if not self.running else self.last_status
            elif created_new_batch and batch_id is not None:
                self.batch_order = [bid for bid in self.batch_order if bid != batch_id]
                self.batch_title_map.pop(title_key, None)
                batch_id = None

            self.load_stats["loaded"] += len(items)
            self.load_stats["duplicates_skipped"] += skipped_local
            self.load_stats["duplicates_from_file"] += skipped_duplicate_file
            self.load_stats["titles"] += 1 if items and created_new_batch else 0
            self.load_stats["invalid_skipped"] += skipped_invalid

        return {
            "batch_id": batch_id,
            "title": title,
            "loaded": len(items),
            "duplicates_skipped": skipped_local,
            "duplicates_from_file": skipped_duplicate_file,
            "invalid_skipped": skipped_invalid,
            "merged": not created_new_batch,
        }

    def remove_batch(self, batch_id: int) -> Dict[str, int]:
        removed = 0
        with self.lock:
            for item in self.queue:
                if item.batch_id == batch_id and item.state == "queued":
                    item.state = "removed"
                    removed += 1
            self._prune_finished_batches_locked()
        return {"removed": removed}

    def remove_domains(self, batch_id: int, raw_domains: str) -> Dict[str, int]:
        domains = {
            normalized
            for line in raw_domains.splitlines()
            if (normalized := self._normalize_domain(line))
        }
        removed = 0
        with self.lock:
            for item in self.queue:
                if item.batch_id == batch_id and item.state == "queued" and item.domain in domains:
                    item.state = "removed"
                    removed += 1
            self._prune_finished_batches_locked()
        return {"removed": removed}

    def add_domains_to_batch(self, batch_id: int, raw_domains: str, duplicate_store: DuplicateStore) -> Dict[str, Any]:
        local_seen = set()
        domains_to_add: List[str] = []
        skipped_local = 0
        skipped_duplicate_file = 0
        skipped_invalid = 0

        with self.lock:
            active_items = [item for item in self.queue if item.batch_id == batch_id and item.state in {"queued", "processing"}]
            if not active_items:
                raise ValueError("Пачка не найдена в активной очереди")
            title = active_items[0].title
            existing = self._existing_active_domains()

        for line in str(raw_domains or "").splitlines():
            domain = self._normalize_domain(line)
            if not domain:
                continue
            if not DOMAIN_RE.match(domain):
                skipped_invalid += 1
                continue
            if domain in local_seen or domain in existing:
                skipped_local += 1
                continue
            if domain in duplicate_store:
                skipped_duplicate_file += 1
                continue
            local_seen.add(domain)
            domains_to_add.append(domain)

        items: List[QueueItem] = []
        with self.lock:
            active_items = [item for item in self.queue if item.batch_id == batch_id and item.state in {"queued", "processing"}]
            if not active_items:
                raise ValueError("Пачка не найдена в активной очереди")
            title = active_items[0].title
            existing_now = self._existing_active_domains()
            for domain in domains_to_add:
                if domain in existing_now:
                    skipped_local += 1
                    continue
                items.append(
                    QueueItem(
                        item_id=self.next_item_id,
                        batch_id=batch_id,
                        title=title,
                        domain=domain,
                    )
                )
                self.next_item_id += 1
                existing_now.add(domain)

            if items:
                if batch_id not in self.batch_order:
                    self.batch_order.append(batch_id)
                self.queue.extend(items)
                self.last_status = "READY" if not self.running else self.last_status

            self.load_stats["loaded"] += len(items)
            self.load_stats["duplicates_skipped"] += skipped_local
            self.load_stats["duplicates_from_file"] += skipped_duplicate_file
            self.load_stats["invalid_skipped"] += skipped_invalid

        return {
            "batch_id": batch_id,
            "title": title,
            "loaded": len(items),
            "duplicates_skipped": skipped_local,
            "duplicates_from_file": skipped_duplicate_file,
            "invalid_skipped": skipped_invalid,
        }

    def move_batch(self, batch_id: int, direction: str) -> Dict[str, Any]:
        if direction not in {"up", "down"}:
            raise ValueError("direction должен быть up или down")
        with self.lock:
            self._prune_finished_batches_locked()
            if batch_id not in self.batch_order:
                raise ValueError("Пачка не найдена в активной очереди")
            current = self.batch_order.index(batch_id)
            target = current - 1 if direction == "up" else current + 1
            if target < 0 or target >= len(self.batch_order):
                return {"moved": False, "position": current + 1}
            self.batch_order[current], self.batch_order[target] = (
                self.batch_order[target],
                self.batch_order[current],
            )
            return {"moved": True, "position": target + 1}

    def _prune_finished_batches_locked(self) -> None:
        active_batch_ids = {item.batch_id for item in self.queue if item.state in {"queued", "processing"}}
        self.batch_order = [bid for bid in self.batch_order if bid in active_batch_ids]
        active_title_map: Dict[str, int] = {}
        for item in self.queue:
            if item.state in {"queued", "processing"}:
                active_title_map.setdefault(item.title.casefold(), item.batch_id)
        self.batch_title_map = active_title_map

    def get_next_item(self) -> Optional[QueueItem]:
        with self.lock:
            self._prune_finished_batches_locked()
            for batch_id in self.batch_order:
                for item in self.queue:
                    if item.batch_id == batch_id and item.state == "queued":
                        item.state = "processing"
                        self.current_item_id = item.item_id
                        self.current_domain = item.domain
                        self.current_title = item.title
                        self.last_status = "WORKING"
                        return item
            self.current_item_id = None
            self.current_domain = ""
            self.current_title = ""
            return None

    def complete_item(
        self,
        item_id: int,
        status: str,
        error: str = "",
        result_fields: Optional[Dict[str, Any]] = None,
    ) -> Optional[ResultRow]:
        with self.lock:
            item = next((x for x in self.queue if x.item_id == item_id), None)
            if item is None:
                return None
            item.state = "done"
            allowed_fields = set(ResultRow.__dataclass_fields__) - {"title", "domain", "status", "error", "finished_at"}
            extras = {key: value for key, value in (result_fields or {}).items() if key in allowed_fields}
            row = ResultRow(title=item.title, domain=item.domain, status=status, error=error, **extras)
            self.results.append(row)
            self.current_item_id = None
            self.current_domain = ""
            self.current_title = ""
            self.last_status = status
            self._prune_finished_batches_locked()
            return row

    def promote_result(self, row: ResultRow) -> None:
        with self.lock:
            try:
                self.results.remove(row)
            except ValueError:
                return
            self.results.append(row)

    def get_batch_summaries(self) -> List[Dict[str, Any]]:
        with self.lock:
            grouped: Dict[int, Dict[str, Any]] = {}
            for item in self.queue:
                info = grouped.setdefault(
                    item.batch_id,
                    {
                        "batch_id": item.batch_id,
                        "title": item.title,
                        "total": 0,
                        "queued": 0,
                        "processing": 0,
                        "done": 0,
                        "removed": 0,
                    },
                )
                info["total"] += 1
                info[item.state] += 1

            result = []
            for batch_id in self.batch_order:
                info = grouped.get(batch_id)
                if not info:
                    continue
                if info["queued"] == 0 and info["processing"] == 0:
                    continue
                result.append(info)
            return result

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            recent_results = list(self.results[-300:])
            recent_ids = {id(row) for row in recent_results}
            pinned_results = [
                row
                for row in self.results
                if (row.status.startswith("PENDING") or row.status.startswith("RETRY")) and id(row) not in recent_ids
            ]
            results_for_ui = pinned_results + recent_results
            counts = {
                "total": len([x for x in self.queue if x.state != "removed"]),
                "processed": len(self.results),
                "remaining": len([x for x in self.queue if x.state in {"queued", "processing"}]),
                "queued": len([x for x in self.queue if x.state == "queued"]),
                "good": sum(1 for r in self.results if r.status == "GOOD"),
                "near": sum(1 for r in self.results if r.status == "GOOD (NEAR THRESHOLD)"),
                "bad": sum(1 for r in self.results if r.status.startswith("BAD")),
                "pending_ai": sum(1 for r in self.results if r.status == PENDING_AI_STATUS),
                "pending_webarchive": sum(1 for r in self.results if r.status == PENDING_WEBARCHIVE_STATUS),
                "not_found": sum(1 for r in self.results if r.status == "Not found"),
                "errors": sum(1 for r in self.results if r.status.startswith("ERROR")),
                "old": sum(1 for r in self.results if r.majestic_status == "GOOD OLD"),
            }
            return {
                "browser_ready": self.browser_ready,
                "browser_launching": self.browser_launching,
                "browser_error": self.browser_error,
                "login_required": self.login_required,
                "running": self.running,
                "paused": self.paused,
                "stop_requested": self.stop_requested,
                "worker_alive": self.worker_alive,
                "browser_recovery_in_progress": self.browser_recovery_in_progress,
                "last_status": self.last_status,
                "current_domain": self.current_domain,
                "current_title": self.current_title,
                "counts": counts,
                "results": [row.__dict__ for row in results_for_ui],
                "logs": self.logs[-160:],
                "queue_batches": self.get_batch_summaries(),
                "ai_queue_size": ai_tasks.qsize(),
                "webarchive_queue_size": webarchive_tasks.qsize(),
                "webarchive_workers": WEBARCHIVE_WORKERS,
                "duplicate_db_size": duplicate_store.size(),
                "sheets_ready": sheets.ready,
                "sheets_error": sheets.last_error,
                "openai_ready": ai_checker.ready,
                "openai_screen_enabled": bool(getattr(ai_checker, "enable_luna_screen", False)),
                "openai_screen_model": ai_checker.screen_model,
                "openai_model": ai_checker.model,
                "quality_model": self.quality_model,
                "quality_model_choices": list(QUALITY_MODEL_CHOICES),
                "openai_base_url": ai_checker.base_url or "https://api.openai.com/v1",
                "openai_error": ai_checker.last_error,
                "openai_notice": ai_checker.model_notice,
                "strict_mode": self.strict_mode,
                "strict_unique_deficit": self.strict_unique_deficit,
                "strict_article_deficit": self.strict_article_deficit,
                "freshness_filter_enabled": self.freshness_filter_enabled,
                "freshness_cutoff_year": self.freshness_cutoff_year,
                "freshness_max_old_share_percent": self.freshness_max_old_share_percent,
                "load_stats": dict(self.load_stats),
            }


state = AppState()
duplicate_store = DuplicateStore(DUPLICATES_FILE)
sheets = GoogleSheetsSink()
ai_tasks: "thread_queue.Queue[AITask]" = thread_queue.Queue()
webarchive_tasks: "thread_queue.Queue[WebArchiveTask]" = thread_queue.Queue()

logger = logging.getLogger("majestic_local_ui")
logger.setLevel(logging.INFO)
logger.handlers.clear()
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
ui_handler = UILogHandler(state)
ui_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(stream_handler)
logger.addHandler(ui_handler)

ai_checker = OpenAIDomainChecker()
ai_checker.model = state.quality_model
ai_checker.strict_mode = state.strict_mode
ai_checker.strict_unique_deficit = state.strict_unique_deficit
ai_checker.strict_article_deficit = state.strict_article_deficit
ai_checker.freshness_filter_enabled = state.freshness_filter_enabled
ai_checker.freshness_cutoff_year = state.freshness_cutoff_year
ai_checker.freshness_max_old_share_percent = state.freshness_max_old_share_percent


class SeleniumSession:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.driver: Optional[webdriver.Chrome] = None
        self.majestic_handle: Optional[str] = None
        self.ui_handle: Optional[str] = None

    def create_driver(self) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
        options.add_argument(fr"--user-data-dir={CHROME_USER_DATA_DIR}")
        options.add_argument(fr"--profile-directory={CHROME_PROFILE_DIRECTORY}")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        if HEADLESS:
            options.add_argument("--headless=new")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        try:
            driver.set_script_timeout(PAGE_LOAD_TIMEOUT)
        except Exception:
            pass
        return driver

    def ensure_started(self) -> None:
        with self.lock:
            if self.driver is not None:
                return
        with state.lock:
            if state.browser_launching:
                return
            state.browser_launching = True
            state.browser_error = ""
        try:
            logger.info("Запускаю Chrome и открываю Majestic + локальный UI")
            driver = self.create_driver()
            driver.get(MAJESTIC_HOME_URL)
            majestic_handle = driver.current_window_handle
            driver.switch_to.new_window("tab")
            ui_handle = driver.current_window_handle
            self._open_ui_when_ready(driver)
            with self.lock:
                self.driver = driver
                self.majestic_handle = majestic_handle
                self.ui_handle = ui_handle
            with state.lock:
                state.browser_ready = True
                state.browser_launching = False
                state.login_required = False
                state.last_status = "BROWSER_READY"
            logger.info("Chrome готов. В первой вкладке Majestic, во второй — интерфейс.")
        except Exception as e:
            with state.lock:
                state.browser_ready = False
                state.browser_launching = False
                state.browser_error = f"{type(e).__name__}: {e}"
                state.last_status = "BROWSER_ERROR"
            logger.error(f"Не удалось запустить Chrome: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())

    def _open_ui_when_ready(self, driver: webdriver.Chrome) -> None:
        ui_url = f"http://{HOST}:{PORT}/"
        for _ in range(60):
            try:
                urllib.request.urlopen(ui_url, timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        driver.get(ui_url)

    def get_driver(self) -> webdriver.Chrome:
        with self.lock:
            if self.driver is None:
                raise RuntimeError("ChromeDriver не запущен")
            return self.driver

    def is_healthy(self) -> bool:
        """Return whether the Selenium session and its Majestic tab still exist."""

        with self.lock:
            driver = self.driver
            majestic_handle = self.majestic_handle
        if driver is None or not majestic_handle:
            return False
        try:
            return majestic_handle in driver.window_handles
        except Exception:
            return False

    def bind_majestic_context(self) -> webdriver.Chrome:
        driver = self.get_driver()
        with self.lock:
            if self.majestic_handle and driver.current_window_handle != self.majestic_handle:
                driver.switch_to.window(self.majestic_handle)
        return driver

    def shutdown(self) -> None:
        with self.lock:
            driver = self.driver
            self.driver = None
            self.majestic_handle = None
            self.ui_handle = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def restart_preserving_app_state(self) -> bool:
        """Replace only ChromeDriver; the in-memory queue/results stay untouched."""

        logger.info("Перезапускаю Chrome/Selenium без сброса очереди.")
        with state.lock:
            state.browser_ready = False
            state.browser_launching = False
            state.browser_error = ""
            state.login_required = False
            state.last_status = "BROWSER_RECOVERY"
        self.shutdown()
        self.ensure_started()
        with state.lock:
            return state.browser_ready


def fetch_article_page_with_browser(
    driver: webdriver.Chrome,
    url: str,
    max_chars: int = 1200,
    target_url: str = "",
) -> Dict[str, Any]:
    """Open a donor URL in a temporary Chrome tab and extract visible article text."""

    result: Dict[str, Any] = {
        "source_url": str(url or ""),
        "status": "BROWSER_ERROR",
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
    original_handle = ""
    opened_handle = ""
    try:
        original_handle = driver.current_window_handle
        driver.switch_to.new_window("tab")
        opened_handle = driver.current_window_handle
        try:
            driver.set_page_load_timeout(min(PAGE_LOAD_TIMEOUT, ARTICLE_BROWSER_PAGE_LOAD_TIMEOUT))
        except Exception:
            pass
        try:
            driver.get(str(url or ""))
        except TimeoutException:
            result["status"] = "BROWSER_TIMEOUT"
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        try:
            WebDriverWait(driver, 7).until(
                lambda drv: drv.execute_script("return document.readyState") in {"interactive", "complete"}
                or bool(drv.find_elements(By.CSS_SELECTOR, "body"))
            )
        except TimeoutException:
            if result["status"] != "BROWSER_TIMEOUT":
                result["status"] = "BROWSER_TIMEOUT"
        data = driver.execute_script(
            """
            const limit = arguments[0] || 1200;
            const targetUrl = String(arguments[1] || '');
            const meta = (name) => {
              const el = document.querySelector(
                `meta[name="${name}"], meta[property="og:${name}"], meta[name="twitter:${name}"]`
              );
              return el ? (el.getAttribute('content') || '') : '';
            };
            const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const cleanHost = (value) => String(value || '').toLowerCase().replace(/^www\\./, '');
            let targetHost = '';
            try { targetHost = cleanHost(new URL(targetUrl).hostname || ''); } catch (e) {}
            const sourceHost = cleanHost(location.hostname || '');
            const hostMatches = (host) => {
              const clean = cleanHost(host);
              return Boolean(targetHost && (clean === targetHost || clean.endsWith(`.${targetHost}`)));
            };
            const areaFor = (el) => {
              const parts = [];
              if (el.closest('article, main')) parts.push('content');
              if (el.closest('p')) parts.push('paragraph');
              if (el.closest('li, ul, ol')) parts.push('list');
              if (el.closest('table, tr, td')) parts.push('table');
              if (el.closest('aside')) parts.push('sidebar');
              if (el.closest('nav')) parts.push('nav');
              if (el.closest('footer')) parts.push('footer');
              if (el.closest('header')) parts.push('header');
              return parts.length ? parts.join('+') : 'body';
            };
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            let externalLinks = 0;
            const targetLinks = [];
            for (const a of anchors) {
              let parsed = null;
              try { parsed = new URL(a.getAttribute('href') || '', location.href); } catch (e) {}
              if (!parsed || !parsed.hostname) continue;
              const host = cleanHost(parsed.hostname);
              if (!sourceHost || host !== sourceHost) externalLinks += 1;
              if (hostMatches(host)) targetLinks.push({el: a, href: parsed.href});
            }
            const preferred = Array.from(document.querySelectorAll('article, main'))
              .map(el => clean(el.innerText || el.textContent || ''))
              .filter(Boolean)
              .join(' ');
            const bodyText = clean((document.body && (document.body.innerText || document.body.textContent)) || '');
            const text = clean(preferred.length >= 240 ? preferred : bodyText);
            const contexts = [];
            const linkTexts = [];
            const areas = [];
            for (const item of targetLinks.slice(0, 3)) {
              const a = item.el;
              const contextNode = a.closest('p, li, td, blockquote, section, article, main, div') || a;
              const linkText = clean(a.innerText || a.textContent || a.getAttribute('title') || '');
              const context = clean(contextNode.innerText || contextNode.textContent || '');
              if (linkText) linkTexts.push(linkText.slice(0, 160));
              if (context) contexts.push(context.slice(0, 700));
              areas.push(areaFor(a));
            }
            const uniq = (arr) => Array.from(new Set(arr.filter(Boolean)));
            const visibleChars = text.length;
            const density = Math.round((externalLinks / Math.max(1, visibleChars / 1000)) * 10) / 10;
            return {
              final_url: String(location.href || ''),
              page_title: clean(document.title || ''),
              description: clean(meta('description')),
              text_excerpt: text.slice(0, limit),
              target_link_found: targetLinks.length > 0,
              target_link_count: targetLinks.length,
              target_link_texts: uniq(linkTexts).join('; ').slice(0, 300),
              link_dom_area: uniq(areas).join('; ').slice(0, 160),
              link_context_excerpt: uniq(contexts).join(' | ').slice(0, 700),
              external_links_count: externalLinks,
              total_links_count: anchors.length,
              visible_text_chars: visibleChars,
              external_link_density: density
            };
            """,
            int(max_chars),
            str(target_url or ""),
        ) or {}
        result["final_url"] = str(data.get("final_url") or driver.current_url or "")[:500]
        result["page_title"] = str(data.get("page_title") or "")[:300]
        result["description"] = str(data.get("description") or "")[:500]
        result["text_excerpt"] = str(data.get("text_excerpt") or "")[:max_chars]
        result["target_link_found"] = bool(data.get("target_link_found", False))
        result["target_link_count"] = int(data.get("target_link_count") or 0)
        result["target_link_texts"] = str(data.get("target_link_texts") or "")[:300]
        result["link_dom_area"] = str(data.get("link_dom_area") or "")[:160]
        result["link_context_excerpt"] = str(data.get("link_context_excerpt") or "")[:700]
        result["external_links_count"] = int(data.get("external_links_count") or 0)
        result["total_links_count"] = int(data.get("total_links_count") or 0)
        result["visible_text_chars"] = int(data.get("visible_text_chars") or 0)
        result["external_link_density"] = data.get("external_link_density") or 0
        if result["text_excerpt"].strip():
            result["status"] = "OK"
        elif result["status"] not in {"BROWSER_TIMEOUT"}:
            result["status"] = "EMPTY_TEXT"
    except Exception as exc:
        result["status"] = "BROWSER_FETCH_ERROR"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        try:
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        except Exception:
            pass
        try:
            if opened_handle and opened_handle in driver.window_handles:
                driver.close()
        except Exception:
            pass
        try:
            if original_handle and original_handle in driver.window_handles:
                driver.switch_to.window(original_handle)
        except Exception:
            pass
    return result


class ArticleBrowserSession:
    """Separate hidden Chrome for donor pages so the visible Majestic window does not steal focus."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.driver: Optional[webdriver.Chrome] = None

    def create_driver(self) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--mute-audio")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--window-size=1280,900")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(ARTICLE_BROWSER_PAGE_LOAD_TIMEOUT)
        try:
            driver.set_script_timeout(ARTICLE_BROWSER_PAGE_LOAD_TIMEOUT)
        except Exception:
            pass
        return driver

    def _shutdown_locked(self) -> None:
        driver = self.driver
        self.driver = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def shutdown(self) -> None:
        with self.lock:
            self._shutdown_locked()

    def fetch(self, url: str, max_chars: int = 1200, target_url: str = "") -> Dict[str, Any]:
        with self.lock:
            if self.driver is None:
                self.driver = self.create_driver()
            try:
                return fetch_article_page_with_browser(self.driver, url, max_chars, target_url)
            except WebDriverException:
                self._shutdown_locked()
                self.driver = self.create_driver()
                return fetch_article_page_with_browser(self.driver, url, max_chars, target_url)


session = SeleniumSession()
article_browser_session = ArticleBrowserSession()


def to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return int(float(match.group(0))) if match else None


def is_logged_out_page(driver: webdriver.Chrome) -> bool:
    try:
        src = (driver.page_source or "").lower()
    except Exception:
        src = ""
    try:
        login_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='password'], form[action*='login']")
    except Exception:
        login_inputs = []
    try:
        title = (driver.title or "").lower()
    except Exception:
        title = ""
    silent_free_trial = (
        "free trial" in title
        or ("sign up for free" in src and "logout" not in src and "my account" not in src)
    )
    return ("you have been logged out" in src) or silent_free_trial or bool(login_inputs)


def is_network_error_page(driver: webdriver.Chrome) -> bool:
    try:
        url = (driver.current_url or "").lower()
        if url.startswith("chrome-error://") or "chromewebdata" in url:
            return True
    except Exception:
        pass
    try:
        if driver.find_elements(By.CSS_SELECTOR, "#main-frame-error, .interstitial-wrapper, #offline-resources"):
            return True
    except Exception:
        pass
    try:
        title = (driver.title or "").lower()
        if title.startswith("this site can’t be reached") or title.startswith("this site can't be reached") or "err_" in title:
            return True
    except Exception:
        pass
    return False


def retry_delay(streak: int = 1) -> None:
    """Back off without navigating; the next stage call opens its own URL."""

    time.sleep(min(2 + streak * 2, 12) + random.randint(0, 2))


def wait_until_login_confirmed(driver: webdriver.Chrome) -> None:
    state.login_event.clear()
    with state.lock:
        state.login_required = True
        state.last_status = "WAITING_LOGIN"
        state.running = False
    logger.error("Нужен ручной логин в Majestic. После логина нажми кнопку 'Продолжить после логина'.")
    while not state.login_event.wait(timeout=0.5):
        with state.lock:
            if state.stop_requested:
                state.login_required = False
                state.last_status = "STOPPING"
                return
    with state.lock:
        state.login_required = False
        state.last_status = "LOGIN_CONFIRMED"
        if not state.stop_requested and any(item.state in {"queued", "processing"} for item in state.queue):
            state.running = True
    try:
        driver.refresh()
        time.sleep(2)
    except Exception:
        pass
    logger.info("Подтверждение логина получено. Продолжаю работу.")


def parse_years(text: str, freshness_cutoff_year: int = 2016) -> tuple[int, int]:
    old = 0
    new = 0
    current_year = datetime.now().year
    cutoff_year = clean_freshness_cutoff_year(freshness_cutoff_year)
    for y in YEAR_RE.findall(text or ""):
        year = int(y)
        if 2000 <= year < cutoff_year:
            old += 1
        elif cutoff_year <= year <= current_year:
            new += 1
    return old, new


def try_enable_dofollow(driver: webdriver.Chrome) -> None:
    try:
        trigger = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//div[contains(@class,'mj-dropdown-trigger')][.//text()[contains(.,'Follow')]]",
                )
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
        trigger.click()
        opt = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(.,'DoFollow')]"))
        )
        opt.click()
        time.sleep(1)
    except Exception:
        pass


def get_rows(driver: webdriver.Chrome):
    WebDriverWait(driver, 20).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div.contextView.js-context-view-row"))
    )
    deadline = time.monotonic() + 8
    last_count = -1
    stable_polls = 0
    rows = []
    while time.monotonic() < deadline:
        rows = driver.find_elements(By.CSS_SELECTOR, "div.contextView.js-context-view-row")
        count = len(rows)
        if count == last_count:
            stable_polls += 1
        else:
            last_count = count
            stable_polls = 0
        if count and stable_polls >= 3:
            return rows
        time.sleep(0.35)
    return rows


def extract_row_json(row_el) -> Dict[str, Any]:
    container = row_el.find_element(By.XPATH, "./ancestor::div[contains(@class,'context-view-row-container')]")
    span = container.find_element(By.CSS_SELECTOR, "span.js-copy-data")
    raw = span.get_attribute("data-context-data") or "{}"
    raw = html.unescape(raw)
    return json.loads(raw)


def is_rootish_link(href: str, domain: str) -> bool:
    if not href:
        return False
    href = href.strip()
    if not href.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlparse(href)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    dom = domain.lower().lstrip("www.")
    if host != dom:
        return False
    path = (parsed.path or "/").strip().rstrip("/")
    if path == "":
        return True
    if path in ("/index.php", "/index.html", "/home"):
        return True
    if path.count("/") == 1 and path[1:].lower() in LOCALE_SEGMENTS:
        return True
    if path.startswith("/index.php/"):
        tail = path[len("/index.php/"):].lower()
        if tail in LOCALE_SEGMENTS:
            return True
    return False


def majestic_base_check(driver: webdriver.Chrome, domain: str) -> str:
    try:
        driver.get(CTX_URL.format(q=domain))
    except TimeoutException:
        logger.warning(f"[{domain}] page load timeout on CTX_URL")
        if is_logged_out_page(driver):
            logger.error(f"[{domain}] timeout + looks like LOGGED_OUT")
            return "LOGGED_OUT"
        if is_network_error_page(driver):
            logger.warning(f"[{domain}] NET_DOWN (chrome interstitial)")
            return "NET_DOWN"
        return "TIMEOUT"
    except WebDriverException:
        raise

    if is_logged_out_page(driver):
        logger.error(f"[{domain}] Majestic appears logged out -> returning LOGGED_OUT")
        return "LOGGED_OUT"
    if is_network_error_page(driver):
        logger.warning(f"[{domain}] NET_DOWN right after load")
        return "NET_DOWN"

    try_enable_dofollow(driver)

    if is_logged_out_page(driver):
        logger.error(f"[{domain}] Majestic appears logged out after interactions")
        return "LOGGED_OUT"
    if is_network_error_page(driver):
        logger.warning(f"[{domain}] NET_DOWN after interactions")
        return "NET_DOWN"

    try:
        rows = get_rows(driver)
    except TimeoutException:
        if is_logged_out_page(driver):
            return "LOGGED_OUT"
        if is_network_error_page(driver):
            return "NET_DOWN"
        return "Not found"

    considered: List[Dict[str, Any]] = []
    ext_big_cnt = 0

    for row in rows:
        try:
            data = extract_row_json(row)
        except Exception:
            continue
        # Keep Majestic "deleted" links in the base context check too:
        # Majestic often marks live backlinks as deleted/lost. Nofollow links
        # still do not count for the drop profile gate.
        if data.get("flagNoFollow"):
            continue
        ext_links = to_int(data.get("sourceOutgoingExternalLinks"))
        tgt_url = data.get("targetUrl") or ""
        ld_val = to_int(data.get("linkDensity"))
        if ext_links is None:
            try:
                badge = row.find_element(By.XPATH, ".//span[contains(@class,'link-type-button') and contains(.,'OF')]")
                match = re.search(r"\b\d+\s*OF\s*(\d+)\b", badge.text, re.I)
                if match:
                    ext_links = int(match.group(1))
            except Exception:
                pass
        is_listing = False
        if ext_links is not None:
            if ext_links >= OUTBOUND_LIMIT_HARD:
                is_listing = True
            elif ext_links >= OUTBOUND_LIMIT_SOFT and ld_val is not None and ld_val >= LD_LISTING_GATE:
                is_listing = True
        if is_listing:
            ext_big_cnt += 1
        considered.append({"target": tgt_url, "extY": ext_links, "ld": ld_val})

    total = len(considered)
    if total == 0:
        if is_logged_out_page(driver):
            return "LOGGED_OUT"
        if is_network_error_page(driver):
            return "NET_DOWN"
        return "Not found"
    if total < MIN_ROWS:
        return "REVIEW:L"

    ratio = ext_big_cnt / total
    root_cnt = sum(1 for r in considered if is_rootish_link(r["target"], domain))
    root_ratio = root_cnt / total
    ld_vals = [r["ld"] for r in considered if r["ld"] is not None]
    if ld_vals and (sum(v >= LD_THRESHOLD_BAD for v in ld_vals) / len(ld_vals) >= LD_SHARE_BAD):
        return "BAD:CONTEXT_DENSITY"
    if ratio > THRESHOLD_EXT_BIG:
        return "BAD:CONTEXT_OUTBOUND"
    if root_ratio < ROOTISH_RATIO_MIN:
        return "BAD:CONTEXT_HOMEPAGE_SHARE"
    return "GOOD"


def check_domain_context(driver: webdriver.Chrome, domain: str, freshness_cutoff_year: int = 2016) -> str:
    verdict = majestic_base_check(driver, domain)
    if verdict not in {"GOOD", "REVIEW:L"}:
        return verdict
    texts: List[str] = []
    for selector in ("div.sourceTitle", "div.sourceURL", "div.mj-context-panel"):
        for el in driver.find_elements(By.CSS_SELECTOR, selector):
            texts.append((el.text or "").strip())
    old_total = 0
    new_total = 0
    for s in texts:
        old_count, new_count = parse_years(s, freshness_cutoff_year)
        old_total += old_count
        new_total += new_count
    if old_total and not new_total:
        return "GOOD OLD"
    if old_total + new_total:
        share_new = new_total / (old_total + new_total)
        if share_new <= 0.6:
            return "GOOD OLD"
    return verdict


def context_status_reason(status: str) -> str:
    reasons = {
        "BAD:CONTEXT_DENSITY": (
            "Стартовый Context-стоп: у большинства dofollow-доноров высокая link density; "
            "похоже на listing/sidebar/sitewide, AI не вызывался."
        ),
        "BAD:CONTEXT_OUTBOUND": (
            "Стартовый Context-стоп: слишком много доноров с большим числом внешних исходящих ссылок; "
            "похоже на каталоги/линкопомойки, AI не вызывался."
        ),
        "BAD:CONTEXT_HOMEPAGE_SHARE": (
            "Стартовый Context-стоп: по Majestic Context меньше 35% dofollow-ссылок ведут на главную/root; "
            "AI не вызывался."
        ),
        "Not found": "Majestic Context не нашёл dofollow-ссылок для базовой проверки.",
    }
    return reasons.get(str(status or ""), "")


def collect_majestic_stage(
    driver: webdriver.Chrome,
    domain: str,
    stage: str,
    collector,
) -> Dict[str, Any]:
    """Retry only the failed report, never the completed Context stage."""

    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_REPORT_RETRIES + 1):
        while True:
            with state.lock:
                if state.stop_requested:
                    raise MajesticReportError(f"{stage}: stop requested")
                paused_now = state.paused
            if not paused_now:
                break
            time.sleep(0.3)
        try:
            return collector()
        except MajesticLoginRequired as exc:
            last_exc = exc
            wait_until_login_confirmed(driver)
            with state.lock:
                if state.stop_requested:
                    raise MajesticReportError(f"{stage}: stop requested") from exc
        except JavascriptException:
            raise
        except (MajesticReportError, TimeoutException, StaleElementReferenceException) as exc:
            last_exc = exc
        if attempt < MAX_REPORT_RETRIES:
            logger.warning(
                f"[{domain}] {stage} failed ({attempt}/{MAX_REPORT_RETRIES}): "
                f"{type(last_exc).__name__}: {str(last_exc)[:240]}; retrying only {stage}"
            )
            retry_delay(attempt)
    raise MajesticReportError(
        f"{stage} failed after {MAX_REPORT_RETRIES} attempts: "
        f"{type(last_exc).__name__}: {str(last_exc)[:240]}"
    ) from last_exc


def ai_result_fields(verdict: DomainVerdict, majestic_status: str = "GOOD") -> Dict[str, Any]:
    if int(getattr(verdict, "api_calls", 0) or 0) > 0:
        ai_status = f"OK {int(verdict.api_calls or 0)}"
    elif str(getattr(verdict, "model", "") or "").upper() == "LOCAL_RULES":
        ai_status = "SKIP:LOCAL"
    else:
        ai_status = "OK 0"
    return {
        "majestic_status": majestic_status,
        "ai_verdict": verdict.verdict,
        "ai_reason": verdict.reason,
        "locale": verdict.locale,
        "locale_source": verdict.locale_source,
        "unique_quality": verdict.unique_quality,
        "article_links": verdict.article_links,
        "homepage_links": verdict.homepage_links,
        "link_year_min": verdict.link_year_min,
        "link_year_max": verdict.link_year_max,
        "link_year_count": verdict.link_year_count,
        "anchor_risk": verdict.anchor_risk,
        "ai_model": verdict.model,
        "ai_input_tokens": verdict.input_tokens,
        "ai_output_tokens": verdict.output_tokens,
        "ai_api_calls": verdict.api_calls,
        "ai_backlinks_sent": verdict.backlinks_sent,
        "ai_anchors_sent": verdict.anchors_sent,
        "ai_early_stop_stage": verdict.early_stop_stage,
        "ai_status": ai_status,
        "webarchive_status": verdict.webarchive_status,
    }


def webarchive_skip_status(archive_result: Any) -> str:
    """Compact UI status for an archive check that could not inspect HTML."""

    errors = [str(error or "") for error in getattr(archive_result, "errors", [])]
    error_text = " ".join(errors).lower()
    snapshots_found = int(getattr(archive_result, "snapshots_found", 0) or 0)
    snapshots_checked = int(getattr(archive_result, "snapshots_checked", 0) or 0)
    if "timeout" in error_text:
        return "SKIP:FETCH_TIMEOUT" if snapshots_found and not snapshots_checked else "SKIP:CDX_TIMEOUT"
    if snapshots_found and not snapshots_checked:
        return "SKIP:FETCH_ERROR"
    if errors:
        return "SKIP:CDX_ERROR"
    return "SKIP:NO_HTML"


def apply_webarchive_spam_gate(verdict: DomainVerdict, domain: str) -> DomainVerdict:
    if not WEBARCHIVE_SPAM_ENABLED:
        verdict.webarchive_status = "OFF"
        return verdict
    if verdict.status not in {"GOOD", "GOOD (NEAR THRESHOLD)"}:
        return verdict
    archive_result = check_webarchive_spam(
        domain,
        locale=verdict.locale,
        years=WEBARCHIVE_SPAM_YEARS,
        max_snapshots=WEBARCHIVE_SPAM_MAX_SNAPSHOTS,
        timeout=WEBARCHIVE_SPAM_TIMEOUT,
        max_chars=WEBARCHIVE_SPAM_MAX_CHARS,
        retries=WEBARCHIVE_SPAM_RETRIES,
    )
    if not archive_result.checked:
        verdict.webarchive_status = webarchive_skip_status(archive_result)
        archive_reason = getattr(archive_result, "reason", verdict.webarchive_status)
        logger.info(f"[{domain}] WebArchive skipped: {archive_reason}")
        return verdict
    if not archive_result.spam:
        verdict.webarchive_status = f"OK {archive_result.snapshots_checked}"
        logger.info(f"[{domain}] WebArchive clean: {archive_result.snapshots_checked} snapshots")
        return verdict

    original_reason = verdict.reason
    archive_reason = archive_result.reason
    verdict.webarchive_status = f"SPAM {archive_result.snapshots_checked}"
    verdict.verdict = "REJECT"
    verdict.status = "BAD:WEBARCHIVE_SPAM"
    verdict.reason = f"{archive_reason}. Предыдущий AI-итог: {original_reason}"
    verdict.hard_stop_reasons = [*list(verdict.hard_stop_reasons or []), archive_reason]
    verdict.early_stop_stage = "webarchive_spam"
    logger.info(f"[{domain}] WebArchive spam gate: {archive_reason}")
    return verdict


def is_unrecoverable_webdriver_exception(exc: WebDriverException) -> bool:
    msg = (str(exc) or "").lower()
    markers = (
        "invalid session id",
        "chrome not reachable",
        "disconnected: not connected to devtools",
        "session deleted because of page crash",
    )
    return any(marker in msg for marker in markers)


def write_results_csv() -> Path:
    with state.lock:
        rows = list(state.results)
    temp_path = RESULTS_CSV_FILE.with_suffix(RESULTS_CSV_FILE.suffix + ".tmp")
    with RESULTS_FILE_LOCK:
        with temp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Title",
                    "Domain",
                    "Status",
                    "MajesticStatus",
                    "AIVerdict",
                    "AIReason",
                    "Locale",
                    "LocaleSource",
                    "UniqueQuality",
                    "ArticleLinks",
                    "HomepageLinks",
                    "LinkYears",
                    "AnchorRisk",
                    "AIModel",
                    "AIAPICalls",
                    "AIBacklinksSent",
                    "AIAnchorsSent",
                    "AIEarlyStopStage",
                    "AIStatus",
                    "Error",
                    "FinishedAt",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row.title,
                        row.domain,
                        row.status,
                        row.majestic_status,
                        row.ai_verdict,
                        row.ai_reason,
                        row.locale,
                        row.locale_source,
                        row.unique_quality,
                        row.article_links,
                        row.homepage_links,
                        format_link_year_range(row),
                        row.anchor_risk,
                        row.ai_model,
                        row.ai_api_calls,
                        row.ai_backlinks_sent,
                        row.ai_anchors_sent,
                        row.ai_early_stop_stage,
                        row.ai_status,
                        row.error,
                        row.finished_at,
                    ]
                )
        os.replace(temp_path, RESULTS_CSV_FILE)
    return RESULTS_CSV_FILE


def write_backlinks_debug(domain: str, report: Dict[str, Any], current_url: str = "") -> None:
    """Keep one inspectable snapshot for diagnosing Majestic DOM/filter changes."""

    payload = {
        "domain": domain,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "current_url": current_url,
        "report": report,
    }
    try:
        BACKLINKS_DEBUG_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"[{domain}] could not save backlink debug snapshot: {type(exc).__name__}: {exc}")


def current_ai_settings_snapshot() -> Dict[str, Any]:
    with state.lock:
        return {
            "quality_model": state.quality_model,
            "strict_mode": state.strict_mode,
            "strict_unique_deficit": state.strict_unique_deficit,
            "strict_article_deficit": state.strict_article_deficit,
            "freshness_filter_enabled": state.freshness_filter_enabled,
            "freshness_cutoff_year": state.freshness_cutoff_year,
            "freshness_max_old_share_percent": state.freshness_max_old_share_percent,
        }


def apply_ai_settings_to_checker(checker: OpenAIDomainChecker, settings: Dict[str, Any]) -> None:
    model = clean_quality_model(settings.get("quality_model"), checker.model)
    if checker.model != model:
        checker.model = model
        checker._model_access_checked = False
        checker.model_notice = ""
    checker.strict_mode = bool(settings.get("strict_mode", False))
    checker.strict_unique_deficit = int(settings.get("strict_unique_deficit", 1))
    checker.strict_article_deficit = int(settings.get("strict_article_deficit", 1))
    checker.freshness_filter_enabled = bool(settings.get("freshness_filter_enabled", True))
    checker.freshness_cutoff_year = clean_freshness_cutoff_year(
        settings.get("freshness_cutoff_year"),
        getattr(checker, "freshness_cutoff_year", 2016),
    )
    checker.freshness_max_old_share_percent = clean_freshness_old_share_percent(
        settings.get("freshness_max_old_share_percent"),
        getattr(checker, "freshness_max_old_share_percent", 50),
    )


def update_row_from_result_fields(
    row: ResultRow,
    status: str,
    error: str = "",
    result_fields: Optional[Dict[str, Any]] = None,
) -> None:
    allowed_fields = set(ResultRow.__dataclass_fields__) - {"title", "domain", "status", "error", "finished_at"}
    row.status = status
    row.error = error
    for key, value in (result_fields or {}).items():
        if key in allowed_fields:
            setattr(row, key, value)
    row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def queue_ai_check(
    row: ResultRow,
    majestic_status: str,
    backlinks_report: Dict[str, Any],
    historic_pages: Dict[str, Any],
    fresh_anchors: Dict[str, Any],
    historic_anchors: Dict[str, Any],
    settings: Dict[str, Any],
) -> None:
    with state.lock:
        row.status = PENDING_AI_STATUS
        row.ai_status = "QUEUED"
        row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_status = "AI_QUEUED"
    ai_tasks.put(
        AITask(
            row=row,
            majestic_status=majestic_status,
            backlinks_report=backlinks_report,
            historic_pages=historic_pages,
            fresh_anchors=fresh_anchors,
            historic_anchors=historic_anchors,
            settings=dict(settings),
        )
    )
    logger.info(f"[{row.domain}] AI queued; continuing with next domain")


def queue_webarchive_check(row: ResultRow) -> bool:
    if not WEBARCHIVE_SPAM_ENABLED or row.status not in FINAL_GOOD_STATUSES:
        return False
    original_status = row.status
    original_reason = row.ai_reason
    with state.lock:
        row.status = PENDING_WEBARCHIVE_STATUS
        row.webarchive_status = "QUEUED"
        row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_status = "WEBARCHIVE_QUEUED"
    webarchive_tasks.put(WebArchiveTask(row=row, original_status=original_status, original_reason=original_reason))
    logger.info(f"[{row.domain}] WebArchive queued; continuing with next domain")
    return True


def webarchive_error_status(compact_status: str) -> str:
    value = str(compact_status or "").upper()
    if "TIMEOUT" in value:
        return "ERROR:WEBARCHIVE_TIMEOUT"
    if "FETCH" in value:
        return "ERROR:WEBARCHIVE_FETCH"
    if "CDX" in value:
        return "ERROR:WEBARCHIVE_CDX"
    return "ERROR:WEBARCHIVE"


def webarchive_should_retry(compact_status: str, reason: str = "") -> bool:
    status = str(compact_status or "").upper()
    text = f"{status} {reason}".upper()
    retry_statuses = {
        "SKIP:CDX_TIMEOUT",
        "SKIP:FETCH_TIMEOUT",
        "SKIP:CDX_ERROR",
        "SKIP:FETCH_ERROR",
    }
    if status in retry_statuses:
        return True
    transient_markers = (
        "TIMEOUT",
        "URLERROR",
        "HTTPERROR",
        "CONNECTION",
        "REMOTE END CLOSED",
        "SERVICE UNAVAILABLE",
        "TOO MANY REQUESTS",
    )
    return any(marker in text for marker in transient_markers)


def requeue_webarchive_task(task: WebArchiveTask, compact_status: str, reason: str) -> None:
    task.attempts += 1
    delay = min(WEBARCHIVE_TIMEOUT_RETRY_DELAY * task.attempts, WEBARCHIVE_RETRY_MAX_DELAY)
    row = task.row
    domain = row.domain
    with state.lock:
        if row.status != PENDING_WEBARCHIVE_STATUS:
            return
        row.webarchive_status = f"QUEUED RETRY {task.attempts}"
        row.error = ""
        row.ai_reason = task.original_reason
        row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_status = "WEBARCHIVE_RETRY"
    write_results_csv()
    logger.warning(
        f"[{domain}] WebArchive transient error; retry #{task.attempts} in {delay}s "
        f"({compact_status}: {str(reason)[:240]})"
    )
    timer = threading.Timer(delay, lambda: webarchive_tasks.put(task))
    timer.daemon = True
    timer.start()


def finalize_webarchive_task(task: WebArchiveTask) -> None:
    row = task.row
    domain = row.domain
    with state.lock:
        if row.status != PENDING_WEBARCHIVE_STATUS:
            return
        row.webarchive_status = "CHECKING"
        row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_status = "WEBARCHIVE_CHECKING"
    logger.info(f"[{domain}] WebArchive background check started")
    try:
        archive_result = check_webarchive_spam(
            domain,
            locale=row.locale,
            years=WEBARCHIVE_SPAM_YEARS,
            max_snapshots=WEBARCHIVE_SPAM_MAX_SNAPSHOTS,
            timeout=WEBARCHIVE_SPAM_TIMEOUT,
            max_chars=WEBARCHIVE_SPAM_MAX_CHARS,
            retries=WEBARCHIVE_SPAM_RETRIES,
        )
    except Exception as exc:
        archive_result = None
        compact_status = "ERROR:WEBARCHIVE_INTERNAL"
        archive_reason = f"WebArchive error: {type(exc).__name__}: {str(exc)[:300]}"
        logger.error(f"[{domain}] WebArchive background error: {archive_reason}")
    else:
        compact_status = webarchive_skip_status(archive_result) if not archive_result.checked else ""
        archive_reason = getattr(archive_result, "reason", compact_status)

    if webarchive_should_retry(compact_status, archive_reason):
        requeue_webarchive_task(task, compact_status, archive_reason)
        return

    should_append_sheets = False
    should_add_duplicate = False
    with state.lock:
        if row.status != PENDING_WEBARCHIVE_STATUS:
            return
        row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if archive_result is None:
            row.status = webarchive_error_status(compact_status)
            row.webarchive_status = compact_status
            row.error = archive_reason
            row.ai_reason = f"AI прошел, но WebArchive завершился ошибкой: {archive_reason}"
        elif not archive_result.checked:
            row.webarchive_status = compact_status
            if compact_status == "SKIP:NO_HTML":
                row.status = task.original_status
                row.error = ""
                row.ai_reason = task.original_reason
                should_append_sheets = row.status in FINAL_GOOD_STATUSES
                should_add_duplicate = True
                logger.info(f"[{domain}] WebArchive has no HTML snapshots; keeping {row.status}")
            else:
                row.status = webarchive_error_status(compact_status)
                row.error = archive_reason
                row.ai_reason = f"AI прошел, но WebArchive не дал надежный результат: {archive_reason}"
                logger.warning(f"[{domain}] WebArchive deferred as error: {compact_status} | {archive_reason}")
        elif not archive_result.spam:
            row.status = task.original_status
            row.webarchive_status = f"OK {archive_result.snapshots_checked}"
            row.error = ""
            row.ai_reason = task.original_reason
            should_append_sheets = row.status in FINAL_GOOD_STATUSES
            should_add_duplicate = True
            logger.info(f"[{domain}] WebArchive clean: {archive_result.snapshots_checked} snapshots")
        else:
            row.status = "BAD:WEBARCHIVE_SPAM"
            row.webarchive_status = f"SPAM {archive_result.snapshots_checked}"
            row.ai_verdict = "REJECT"
            row.ai_reason = f"{archive_result.reason}. Предыдущий AI-итог: {task.original_reason}"
            row.ai_early_stop_stage = "webarchive_spam"
            row.error = ""
            should_add_duplicate = True
            logger.info(f"[{domain}] WebArchive spam gate: {archive_result.reason}")
        state.last_status = "WEBARCHIVE_DONE" if not state.running else state.last_status

    state.promote_result(row)
    if should_add_duplicate:
        duplicate_store.add(domain)
    if should_append_sheets:
        sheets.append_good(row)
    write_results_csv()


def finalize_ai_task(task: AITask, checker: OpenAIDomainChecker) -> None:
    row = task.row
    domain = row.domain
    with state.lock:
        if row.status != PENDING_AI_STATUS:
            return
        row.ai_status = "CHECKING"
        row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.last_status = "AI_CHECKING"
    logger.info(f"[{domain}] AI background check started")

    try:
        apply_ai_settings_to_checker(checker, task.settings)
        if not checker.ready:
            raise RuntimeError(checker.last_error or "OpenAI client is not ready")
        verdict = checker.evaluate(
            domain=domain,
            title=row.title,
            backlinks_report=task.backlinks_report,
            fresh_anchors=task.fresh_anchors,
            historic_anchors=task.historic_anchors,
            majestic_status=task.majestic_status,
            historic_pages_report=task.historic_pages,
            browser_page_fetcher=article_browser_session.fetch,
        )
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {str(exc)[:500]}"
        with state.lock:
            update_row_from_result_fields(
                row,
                "ERROR:AI",
                err_text,
                {
                    "majestic_status": task.majestic_status,
                    "ai_status": "ERROR",
                    "ai_reason": err_text,
                },
            )
            state.last_status = "AI_ERROR" if not state.running else state.last_status
        logger.error(f"[{domain}] OpenAI background error: {err_text}")
        state.promote_result(row)
        write_results_csv()
        return

    result_fields = ai_result_fields(verdict, majestic_status=task.majestic_status)
    with state.lock:
        if row.status != PENDING_AI_STATUS:
            return
        update_row_from_result_fields(row, verdict.status, "", result_fields)
        state.last_status = "AI_DONE" if not state.running else state.last_status

    logger.info(
        f"[{domain}] AI => {verdict.verdict} | "
        f"metric={verdict.unique_quality}/{verdict.article_links}/{verdict.homepage_links} | "
        f"anchors={verdict.anchor_risk} | calls={verdict.api_calls} | "
        f"stop={verdict.early_stop_stage} | majestic={task.majestic_status}"
    )

    webarchive_queued = queue_webarchive_check(row)
    if not webarchive_queued:
        state.promote_result(row)
        if not row.status.startswith("ERROR"):
            duplicate_store.add(row.domain)
        if row.status in FINAL_GOOD_STATUSES:
            sheets.append_good(row)
        write_results_csv()
    else:
        write_results_csv()


def ai_worker(worker_id: int) -> None:
    checker = OpenAIDomainChecker()
    logger.info(f"AI worker #{worker_id} запущен.")
    while True:
        task = ai_tasks.get()
        try:
            finalize_ai_task(task, checker)
        finally:
            ai_tasks.task_done()


def webarchive_worker(worker_id: int = 1) -> None:
    logger.info("WebArchive worker запущен.")
    while True:
        task = webarchive_tasks.get()
        try:
            finalize_webarchive_task(task)
        finally:
            webarchive_tasks.task_done()


def majestic_polite_delay(processed_count: int) -> None:
    delay_min = min(MAJESTIC_DOMAIN_DELAY_MIN, MAJESTIC_DOMAIN_DELAY_MAX)
    delay_max = max(MAJESTIC_DOMAIN_DELAY_MIN, MAJESTIC_DOMAIN_DELAY_MAX)
    delay = random.randint(delay_min, delay_max) if delay_max > 0 else 0
    if MAJESTIC_LONG_PAUSE_EVERY and processed_count > 0 and processed_count % MAJESTIC_LONG_PAUSE_EVERY == 0:
        long_min = min(MAJESTIC_LONG_PAUSE_MIN, MAJESTIC_LONG_PAUSE_MAX)
        long_max = max(MAJESTIC_LONG_PAUSE_MIN, MAJESTIC_LONG_PAUSE_MAX)
        delay += random.randint(long_min, long_max) if long_max > 0 else 0
    if delay <= 0:
        return
    with state.lock:
        previous_status = state.last_status
        state.last_status = "MAJESTIC_THROTTLE"
    delay_until = time.monotonic() + delay
    while time.monotonic() < delay_until:
        with state.lock:
            if state.stop_requested or state.paused:
                break
        time.sleep(0.25)
    with state.lock:
        if state.last_status == "MAJESTIC_THROTTLE":
            state.last_status = previous_status


def domain_worker() -> None:
    with state.lock:
        state.worker_alive = True
    logger.info("Worker запущен.")

    driver: Optional[webdriver.Chrome] = None
    majestic_processed_count = 0
    while True:
        time.sleep(0.2)
        with state.lock:
            if state.stop_requested:
                state.stop_requested = False
                state.running = False
                state.paused = False
                state.current_domain = ""
                state.current_title = ""
                state.last_status = "STOPPED"
                logger.info("Обработка остановлена.")
                continue
            if state.paused or not state.running:
                continue
        try:
            # Always rebind: a repeated Start may have replaced Chrome while
            # preserving the in-memory queue.
            driver = session.bind_majestic_context()
        except Exception as e:
            with state.lock:
                state.running = False
                state.last_status = "BROWSER_ERROR"
                state.browser_ready = False
                state.browser_error = f"{type(e).__name__}: {e}"
            logger.error(f"Браузер недоступен: {type(e).__name__}: {e}")
            driver = None
            continue

        item = state.get_next_item()
        if item is None:
            with state.lock:
                state.running = False
                state.current_domain = ""
                state.current_title = ""
                state.last_status = "DONE"
            logger.info("Очередь завершена.")
            write_results_csv()
            continue

        streak = 0
        browser_recoveries = 0
        err_text = ""
        status = "ERROR:UNKNOWN"
        result_fields: Dict[str, Any] = {}
        pending_ai_task: Optional[Dict[str, Any]] = None
        used_majestic = False

        while True:
            with state.lock:
                if state.stop_requested:
                    break
                paused_now = state.paused
            if paused_now:
                time.sleep(0.3)
                continue

            name_verdict = local_domain_name_precheck(item.domain, item.title)
            if name_verdict is not None:
                status = name_verdict.status
                result_fields = ai_result_fields(name_verdict, majestic_status="LOCAL")
                err_text = ""
                logger.info(f"[{item.domain}] local early stop before Majestic: {name_verdict.status}")
                break

            if streak >= MAX_RETRIES_PER_DOMAIN:
                if browser_recoveries < MAX_BROWSER_RECOVERIES_PER_DOMAIN:
                    browser_recoveries += 1
                    logger.warning(
                        f"[{item.domain}] retry limit reached; восстанавливаю Chrome "
                        f"({browser_recoveries}/{MAX_BROWSER_RECOVERIES_PER_DOMAIN}) без сброса очереди"
                    )
                    if session.restart_preserving_app_state():
                        driver = session.bind_majestic_context()
                        # Permit only one final attempt after browser recovery.
                        # Resetting to zero used to allow a second full cycle.
                        streak = MAX_RETRIES_PER_DOMAIN - 1
                        continue
                status = "ERROR:RETRY_LIMIT"
                err_text = f"retry_limit_reached={streak}; browser_recoveries={browser_recoveries}"
                logger.error(f"[{item.domain}] retry limit reached")
                break

            try:
                ai_settings = current_ai_settings_snapshot()
                apply_ai_settings_to_checker(ai_checker, ai_settings)
                context_freshness_cutoff_year = ai_settings["freshness_cutoff_year"]
                used_majestic = True
                status = check_domain_context(driver, item.domain, context_freshness_cutoff_year)
                if status == "LOGGED_OUT":
                    wait_until_login_confirmed(driver)
                    streak += 1
                    retry_delay(streak)
                    continue
                if status in ("NET_DOWN", "TIMEOUT"):
                    streak += 1
                    logger.warning(
                        f"[{item.domain}] {status} -> reload and retry (streak={streak}) | "
                        f"url={getattr(driver, 'current_url', '')} | title={getattr(driver, 'title', '')}"
                    )
                    retry_delay(streak)
                    continue
                if status not in AI_ELIGIBLE_MAJESTIC_STATUSES:
                    result_fields = {"majestic_status": status}
                    context_reason = context_status_reason(status)
                    if context_reason:
                        result_fields.update(
                            {
                                "ai_verdict": "REJECT",
                                "ai_reason": context_reason,
                                "ai_status": "SKIP:CONTEXT",
                            }
                        )
                if status in AI_ELIGIBLE_MAJESTIC_STATUSES:
                    majestic_status = status
                    result_fields = {"majestic_status": majestic_status}
                    if not ai_checker.ready:
                        status = "ERROR:AI_NOT_CONFIGURED"
                        err_text = ai_checker.last_error
                        logger.error(f"[{item.domain}] OpenAI disabled: {err_text}")
                        break

                    with state.lock:
                        state.last_status = "BACKLINKS_FRESH_DOFOLLOW"
                    logger.info(
                        f"[{item.domain}] collecting Backlinks Fresh: DoFollow, 1 per domain, deleted included"
                    )
                    backlinks_report = collect_majestic_stage(
                        driver,
                        item.domain,
                        "Backlinks Fresh DoFollow",
                        lambda: collect_backlinks(
                            driver,
                            item.domain,
                            max_rows=MAJESTIC_MAX_BACKLINKS,
                        ),
                    )
                    write_backlinks_debug(
                        item.domain,
                        backlinks_report,
                        current_url=str(getattr(driver, "current_url", "") or ""),
                    )
                    verdict = ai_checker.precheck_backlinks(
                        domain=item.domain,
                        title=item.title,
                        backlinks_report=backlinks_report,
                    )
                    if verdict is not None:
                        logger.info(
                            f"[{item.domain}] local early stop before Anchor pages and API: {verdict.status}"
                        )
                    else:
                        with state.lock:
                            state.last_status = "PAGES_HISTORIC"
                        logger.info(f"[{item.domain}] collecting Pages Historic (IndexDataSource=H)")
                        historic_pages = collect_majestic_stage(
                            driver,
                            item.domain,
                            "Pages Historic",
                            lambda: collect_pages(
                                driver,
                                item.domain,
                                "H",
                                max_rows=MAJESTIC_MAX_HISTORIC_PAGES,
                            ),
                        )
                        verdict = ai_checker.precheck_historic_pages(
                            domain=item.domain,
                            title=item.title,
                            pages_report=historic_pages,
                        )
                        if verdict is not None:
                            logger.info(
                                f"[{item.domain}] local early stop from Historic Pages before Anchor/API: {verdict.status}"
                            )
                    if verdict is None:
                        with state.lock:
                            state.last_status = "ANCHORS_FRESH"
                        logger.info(f"[{item.domain}] collecting Anchor Text Fresh (IndexDataSource=F)")
                        fresh_anchors = collect_majestic_stage(
                            driver,
                            item.domain,
                            "Anchor Text Fresh",
                            lambda: collect_anchor_text(
                                driver,
                                item.domain,
                                "F",
                                max_rows=MAJESTIC_MAX_ANCHORS,
                            ),
                        )

                        with state.lock:
                            state.last_status = "ANCHORS_HISTORIC"
                        logger.info(f"[{item.domain}] collecting Anchor Text Historic (IndexDataSource=H)")
                        historic_anchors = collect_majestic_stage(
                            driver,
                            item.domain,
                            "Anchor Text Historic",
                            lambda: collect_anchor_text(
                                driver,
                                item.domain,
                                "H",
                                max_rows=MAJESTIC_MAX_ANCHORS,
                            ),
                        )

                        with state.lock:
                            state.last_status = "AI_QUEUE"
                        logger.info(
                            f"[{item.domain}] staged AI queue: backlinks={len(backlinks_report['rows'])}, "
                            f"anchors Fresh={len(fresh_anchors['rows'])}, Historic={len(historic_anchors['rows'])}, "
                            f"pages Historic={len(historic_pages['rows'])}"
                        )
                        status = PENDING_AI_STATUS
                        result_fields = {
                            "majestic_status": majestic_status,
                            "ai_status": "QUEUED",
                        }
                        pending_ai_task = {
                            "majestic_status": majestic_status,
                            "backlinks_report": backlinks_report,
                            "historic_pages": historic_pages,
                            "fresh_anchors": fresh_anchors,
                            "historic_anchors": historic_anchors,
                            "settings": ai_settings,
                        }
                    if verdict is not None:
                        status = verdict.status
                        result_fields = ai_result_fields(verdict, majestic_status=majestic_status)
                    err_text = ""
                logger.info(f"[{item.domain}] => {status}")
                err_text = ""
                break
            except MajesticLoginRequired:
                wait_until_login_confirmed(driver)
                streak += 1
                retry_delay(streak)
                continue
            except MajesticReportError as e:
                status = "ERROR:MAJESTIC_REPORT"
                err_text = f"{type(e).__name__}: {str(e)[:300]}"
                logger.error(f"[{item.domain}] Majestic report stage stopped: {err_text}")
                break
            except (TimeoutException, StaleElementReferenceException) as e:
                streak += 1
                err_text = f"{type(e).__name__}: {str(e)[:300]}"
                logger.warning(f"[{item.domain}] transient {type(e).__name__}: {e}")
                logger.warning(traceback.format_exc())
                if is_logged_out_page(driver):
                    wait_until_login_confirmed(driver)
                retry_delay(streak)
                continue
            except JavascriptException as e:
                status = "ERROR:PARSER"
                err_text = f"{type(e).__name__}: {str(e)[:300]}"
                logger.error(
                    f"[{item.domain}] parser JavaScript failed; retry loop stopped: {err_text}"
                )
                logger.error(traceback.format_exc())
                break
            except WebDriverException as e:
                err_text = f"{type(e).__name__}: {str(e)[:300]}"
                logger.error(f"[{item.domain}] WebDriverException: {e}")
                logger.error(traceback.format_exc())
                if is_unrecoverable_webdriver_exception(e):
                    with state.lock:
                        state.running = False
                        state.browser_ready = False
                        state.browser_error = err_text
                        state.last_status = "BROWSER_DEAD"
                    logger.error("ChromeDriver session looks dead. Перезапусти скрипт.")
                    status = "ERROR:BROWSER_DEAD"
                    driver = None
                    break
                streak += 1
                if is_logged_out_page(driver):
                    wait_until_login_confirmed(driver)
                retry_delay(streak)
                continue
            except Exception as e:
                status = "ERROR:UNEXPECTED"
                err_text = f"{type(e).__name__}: {str(e)[:300]}"
                logger.error(f"[{item.domain}] error {type(e).__name__}: {e}")
                logger.error(traceback.format_exc())
                if is_logged_out_page(driver):
                    wait_until_login_confirmed(driver)
                break

        with state.lock:
            if state.stop_requested:
                if item.state == "processing":
                    item.state = "queued"
                state.stop_requested = False
                state.running = False
                state.current_domain = ""
                state.current_title = ""
                state.last_status = "STOPPED"
                logger.info("Остановка подтверждена во время обработки домена.")
                continue

        row = state.complete_item(item.item_id, status, err_text, result_fields=result_fields)
        if row is not None:
            if pending_ai_task is not None and row.status == PENDING_AI_STATUS:
                queue_ai_check(
                    row,
                    majestic_status=str(pending_ai_task["majestic_status"]),
                    backlinks_report=pending_ai_task["backlinks_report"],
                    historic_pages=pending_ai_task["historic_pages"],
                    fresh_anchors=pending_ai_task["fresh_anchors"],
                    historic_anchors=pending_ai_task["historic_anchors"],
                    settings=pending_ai_task["settings"],
                )
            else:
                webarchive_queued = queue_webarchive_check(row)
                if webarchive_queued:
                    write_results_csv()
                    if used_majestic:
                        majestic_processed_count += 1
                        majestic_polite_delay(majestic_processed_count)
                    continue
                if not row.status.startswith("ERROR"):
                    duplicate_store.add(row.domain)
                if row.status in FINAL_GOOD_STATUSES:
                    sheets.append_good(row)
        write_results_csv()
        if used_majestic:
            majestic_processed_count += 1
            majestic_polite_delay(majestic_processed_count)


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/batches")
def api_add_batch():
    payload = request.get_json(force=True)
    title = payload.get("title", "")
    domains = payload.get("domains", "")
    stats = state.add_batch(title=title, raw_domains=domains, duplicate_store=duplicate_store)
    logger.info(
        f"Пачка '{stats['title']}' добавлена: {stats['loaded']}, локальных дублей: {stats['duplicates_skipped']}, "
        f"из txt: {stats['duplicates_from_file']}, невалидных: {stats['invalid_skipped']}"
    )
    return jsonify({"ok": True, **stats})


@app.delete("/api/batches/<int:batch_id>")
def api_remove_batch(batch_id: int):
    stats = state.remove_batch(batch_id)
    logger.info(f"Пачка {batch_id} удалена из очереди. Удалено доменов: {stats['removed']}")
    return jsonify({"ok": True, **stats})


@app.post("/api/batches/<int:batch_id>/remove-domains")
def api_remove_domains(batch_id: int):
    payload = request.get_json(force=True)
    domains = payload.get("domains", "")
    stats = state.remove_domains(batch_id, domains)
    logger.info(f"Из пачки {batch_id} убрано доменов: {stats['removed']}")
    return jsonify({"ok": True, **stats})


@app.post("/api/batches/<int:batch_id>/add-domains")
def api_add_domains_to_batch(batch_id: int):
    payload = request.get_json(force=True)
    domains = payload.get("domains", "")
    try:
        stats = state.add_domains_to_batch(batch_id, domains, duplicate_store)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    logger.info(
        f"В пачку {batch_id} добавлено доменов: {stats['loaded']}, локальных дублей: {stats['duplicates_skipped']}, "
        f"из txt: {stats['duplicates_from_file']}, невалидных: {stats['invalid_skipped']}"
    )
    return jsonify({"ok": True, **stats})


@app.post("/api/batches/<int:batch_id>/move")
def api_move_batch(batch_id: int):
    payload = request.get_json(force=True)
    try:
        stats = state.move_batch(batch_id, str(payload.get("direction", "")))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    logger.info(f"Пачка {batch_id} перемещена: {payload.get('direction')} (позиция {stats['position']})")
    return jsonify({"ok": True, **stats})


@app.get("/api/prompts")
def api_get_prompts():
    try:
        return jsonify({"ok": True, **read_active_prompts()})
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Не удалось прочитать промпты: {exc}"}), 500


@app.post("/api/prompts")
def api_save_prompts():
    payload = request.get_json(force=True)
    try:
        values = save_active_prompts(payload)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    logger.info("Действующие AI-промпты обновлены через интерфейс.")
    return jsonify({"ok": True, **values})


@app.post("/api/duplicates/remove")
def api_remove_duplicates():
    payload = request.get_json(force=True)
    try:
        stats = duplicate_store.remove_many(str(payload.get("domains", "")))
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    logger.info(
        f"Из базы дублей удалено доменов: {stats['removed']}; "
        f"не найдено: {stats['not_found']}; осталось: {stats['remaining']}"
    )
    return jsonify({"ok": True, **stats})


@app.post("/api/settings")
def api_update_settings():
    payload = request.get_json(force=True)
    settings = state.set_strict_settings(
        bool(payload.get("strict_mode", False)),
        payload.get("strict_unique_deficit"),
        payload.get("strict_article_deficit"),
        payload.get("quality_model"),
        payload.get("freshness_filter_enabled"),
        payload.get("freshness_cutoff_year"),
        payload.get("freshness_max_old_share_percent"),
    )
    if ai_checker.model != settings["quality_model"]:
        ai_checker.model = settings["quality_model"]
        ai_checker._model_access_checked = False
        ai_checker.model_notice = ""
    ai_checker.strict_mode = settings["strict_mode"]
    ai_checker.strict_unique_deficit = settings["strict_unique_deficit"]
    ai_checker.strict_article_deficit = settings["strict_article_deficit"]
    ai_checker.freshness_filter_enabled = settings["freshness_filter_enabled"]
    ai_checker.freshness_cutoff_year = settings["freshness_cutoff_year"]
    ai_checker.freshness_max_old_share_percent = settings["freshness_max_old_share_percent"]
    logger.info(
        (
            "Strict mode включен: NEAR допускает максимум "
            f"-{settings['strict_unique_deficit']} уникальных и "
            f"-{settings['strict_article_deficit']} статейных."
        )
        if settings["strict_mode"]
        else "Strict mode выключен: NEAR вернулся к допуску -3 уникальных и -2 статейных."
    )
    logger.info(f"Модель AI-анализа ссылок: {settings['quality_model']}")
    logger.info(
        (
            f"Freshness filter включен: ссылки до {settings['freshness_cutoff_year']} "
            f"максимум {settings['freshness_max_old_share_percent']}%."
        )
        if settings["freshness_filter_enabled"]
        else "Freshness filter выключен."
    )
    return jsonify({"ok": True, **settings})


def recover_browser_then_start() -> None:
    """Background recovery used by repeated Start without resetting the queue."""

    try:
        recovered = session.restart_preserving_app_state()
    except Exception as exc:
        recovered = False
        with state.lock:
            state.browser_error = f"{type(exc).__name__}: {exc}"
        logger.error(f"Не удалось восстановить Chrome: {type(exc).__name__}: {exc}")

    with state.lock:
        state.browser_recovery_in_progress = False
        has_queue = any(item.state in {"queued", "processing"} for item in state.queue)
        should_run = recovered and has_queue and not state.stop_requested
        state.running = should_run
        state.paused = False
        if should_run:
            state.last_status = "RUNNING"
        elif recovered and not has_queue:
            state.last_status = "DONE"
        elif not recovered:
            state.last_status = "BROWSER_ERROR"
    if should_run:
        logger.info("Chrome восстановлен. Продолжаю сохранённую очередь.")


@app.post("/api/start")
def api_start():
    start_recovery = False
    with state.lock:
        check_existing_browser = state.has_started_once and state.browser_ready
    existing_browser_healthy = session.is_healthy() if check_existing_browser else False

    with state.lock:
        has_queue = any(item.state in {"queued", "processing"} for item in state.queue)
        if not has_queue:
            return jsonify({"ok": False, "error": "Сначала добавь домены в очередь"}), 400
        if state.browser_recovery_in_progress:
            return jsonify({"ok": True, "recovering_browser": True})
        if state.running and state.stop_requested:
            return jsonify({"ok": False, "error": "Сначала дождись завершения остановки"}), 409
        if state.running:
            state.paused = False
            state.last_status = "RUNNING"
            return jsonify({"ok": True, "already_running": True})
        start_recovery = state.has_started_once and not existing_browser_healthy
        if not state.has_started_once and not state.browser_ready:
            return jsonify({"ok": False, "error": "Chrome еще не готов"}), 400
        state.has_started_once = True
        state.paused = False
        state.stop_requested = False
        if start_recovery:
            state.running = False
            state.browser_recovery_in_progress = True
            state.last_status = "BROWSER_RECOVERY"
        else:
            state.running = True
            state.last_status = "RUNNING"
    if start_recovery:
        logger.info("Повторный старт: сначала обновляю Chrome/Selenium, очередь сохранена.")
        threading.Thread(target=recover_browser_then_start, daemon=True).start()
        return jsonify({"ok": True, "recovering_browser": True})
    logger.info("Старт обработки.")
    return jsonify({"ok": True, "recovering_browser": False})


@app.post("/api/pause")
def api_pause():
    with state.lock:
        state.paused = not state.paused
        state.last_status = "PAUSED" if state.paused else "RUNNING"
        paused = state.paused
    logger.info("Пауза включена." if paused else "Пауза снята.")
    return jsonify({"ok": True, "paused": paused})


@app.post("/api/stop")
def api_stop():
    with state.lock:
        state.stop_requested = True
        state.last_status = "STOPPING"
    # Wake a worker that may be waiting for manual Majestic login.
    state.login_event.set()
    logger.info("Запрошена остановка.")
    return jsonify({"ok": True})


@app.post("/api/login-confirm")
def api_login_confirm():
    with state.lock:
        state.login_required = False
        state.last_status = "LOGIN_CONFIRMED"
    state.login_event.set()
    logger.info("Пользователь подтвердил ручной логин.")
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    return jsonify(state.get_snapshot())


@app.post("/api/logs/clear")
def api_clear_logs():
    state.clear_logs()
    return jsonify({"ok": True})


@app.get("/api/download/results.csv")
def api_download_results():
    with RESULTS_FILE_LOCK:
        write_results_csv()
        payload = RESULTS_CSV_FILE.read_bytes()
    return send_file(
        io.BytesIO(payload),
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="results.csv",
    )


def ensure_background_threads() -> None:
    if not state.processing_thread or not state.processing_thread.is_alive():
        state.processing_thread = threading.Thread(target=domain_worker, daemon=True)
        state.processing_thread.start()
    state.ai_threads = [thread for thread in state.ai_threads if thread.is_alive()]
    while len(state.ai_threads) < OPENAI_AI_WORKERS:
        worker_id = len(state.ai_threads) + 1
        thread = threading.Thread(target=ai_worker, args=(worker_id,), daemon=True)
        state.ai_threads.append(thread)
        thread.start()
    state.webarchive_threads = [thread for thread in state.webarchive_threads if thread.is_alive()]
    while WEBARCHIVE_SPAM_ENABLED and len(state.webarchive_threads) < WEBARCHIVE_WORKERS:
        worker_id = len(state.webarchive_threads) + 1
        thread = threading.Thread(target=webarchive_worker, args=(worker_id,), daemon=True)
        state.webarchive_threads.append(thread)
        thread.start()
    if not state.browser_ready and not state.browser_launching:
        threading.Thread(target=session.ensure_started, daemon=True).start()
    if GOOGLE_SHEETS_ENABLED:
        threading.Thread(target=sheets.init, daemon=True).start()


if __name__ == "__main__":
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    atexit.register(article_browser_session.shutdown)
    ensure_background_threads()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
