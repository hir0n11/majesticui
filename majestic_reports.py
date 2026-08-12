"""Selenium collectors for Majestic Backlinks and Anchor Text reports."""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qs, urlencode, urlparse

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


REPORT_BASE = "https://majestic.com/reports/site-explorer"
PAGE_SIZE = 50


class MajesticReportError(RuntimeError):
    """Raised when a report cannot be collected reliably."""


class MajesticLoginRequired(MajesticReportError):
    """Raised when Majestic redirected the browser to login."""


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"-?\d[\d\s,.]*", str(value))
    if not match:
        return None
    raw = re.sub(r"[^\d-]", "", match.group(0))
    try:
        return int(raw)
    except ValueError:
        return None


def _source_domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def build_backlinks_url(domain: str, offset: int = 0) -> str:
    params = {
        "q": domain,
        "scope": "domain",
        "IndexDataSource": "F",
        # Evaluate one representative backlink per referring domain.  Counting
        # every URL from the same donor inflates the drop-domain profile.
        "MaxSourceUrlsPerRefDomain": "1",
        # Keep Majestic "deleted/lost" backlinks visible: Majestic often marks
        # working links as lost, and this slice must be checked too.
        "removeDeleted": "0",
    }
    if offset:
        params["s"] = str(offset)
    return f"{REPORT_BASE}/top-backlinks?{urlencode(params)}"


def build_anchor_url(domain: str, index_source: str, offset: int = 0) -> str:
    source = index_source.upper()
    if source not in {"F", "H"}:
        raise ValueError("index_source must be F (Fresh) or H (Historic)")
    params = {
        "q": domain,
        "scope": "domain",
        "IndexDataSource": source,
        "textmode": "0",
        "filteranchors": "",
    }
    if offset:
        params["s"] = str(offset)
    return f"{REPORT_BASE}/anchor-text?{urlencode(params)}"


def build_pages_url(domain: str, index_source: str = "H", offset: int = 0) -> str:
    source = index_source.upper()
    if source not in {"F", "H"}:
        raise ValueError("index_source must be F (Fresh) or H (Historic)")
    params = {
        "q": domain,
        "scope": "domain",
        "IndexDataSource": source,
    }
    if offset:
        params["s"] = str(offset)
    return f"{REPORT_BASE}/top-pages?{urlencode(params)}"


def _is_login_page(driver) -> bool:
    try:
        source = (driver.page_source or "").lower()
    except Exception:
        source = ""
    try:
        title = (driver.title or "").lower()
    except Exception:
        title = ""
    try:
        has_login = bool(driver.find_elements(By.CSS_SELECTOR, "input[type='password'], form[action*='login']"))
    except Exception:
        has_login = False
    silent_free_trial = (
        "free trial" in title
        or ("sign up for free" in source and "logout" not in source and "my account" not in source)
    )
    return has_login or silent_free_trial or "you have been logged out" in source


def _wait_for_report(driver, table_selector: str, timeout: int = 25) -> None:
    def ready(drv):
        if _is_login_page(drv):
            return True
        if drv.find_elements(By.CSS_SELECTOR, table_selector):
            return True
        body = (drv.find_element(By.TAG_NAME, "body").text or "").lower()
        return "no results" in body or "no backlinks" in body or "no anchor text" in body

    try:
        WebDriverWait(driver, timeout).until(ready)
    except TimeoutException as exc:
        raise MajesticReportError(f"Majestic report did not become ready: {table_selector}") from exc
    if _is_login_page(driver):
        raise MajesticLoginRequired("Majestic login is required")


def verify_index_source(driver, expected: str) -> None:
    expected_value = expected.upper()
    try:
        actual = parse_qs(urlparse(driver.current_url).query).get("IndexDataSource", [""])[0].upper()
    except Exception as exc:
        raise MajesticReportError("Could not verify Majestic index source") from exc
    if actual != expected_value:
        raise MajesticReportError(
            f"Majestic index mismatch: expected IndexDataSource={expected_value}, got {actual or 'missing'}"
        )


def _visible(elements):
    for element in elements:
        try:
            if element.is_displayed():
                yield element
        except Exception:
            continue


DOFOLLOW_ACTIVE_SELECTORS = (
    ".filter-tags .active",
    ".filters .selected",
    ".filters .active",
    ".filter-pill",
    ".selected-filter",
)

DOFOLLOW_ACTIVE_XPATHS = (
    # Current Majestic UI renders the applied filter as a compact tag with
    # this exact label. It is not consistently marked with `.active`.
    "//*[normalize-space(.)='Follow (DoFollow)']",
    "//*[contains(@class,'filter') and normalize-space(.)='Follow (DoFollow)']",
)


def _dofollow_is_active(driver) -> bool:
    for selector in DOFOLLOW_ACTIVE_SELECTORS:
        for element in _visible(driver.find_elements(By.CSS_SELECTOR, selector)):
            if "dofollow" in _clean_text(element.text).casefold():
                return True
    for xpath in DOFOLLOW_ACTIVE_XPATHS:
        for element in _visible(driver.find_elements(By.XPATH, xpath)):
            if _clean_text(element.text).casefold() == "follow (dofollow)":
                return True
    return False


def _backlinks_table_signature(driver) -> str:
    return str(
        driver.execute_script(
            """
            const rows = Array.from(document.querySelectorAll(
              '#vue-backlinks-table tbody > tr.odd, #vue-backlinks-table tbody > tr.even'
            )).filter(row => !row.classList.contains('js-copy-ignore'));
            return rows.map(row => {
              const source = row.querySelector('td.backlink-source .sourceURL');
              const target = row.querySelector('td.a-backlink .targetURL');
              return `${(source && source.innerText) || ''}|${(target && target.innerText) || ''}`;
            }).join(String.fromCharCode(10));
            """
        )
        or ""
    )


def _wait_for_backlinks_settle(
    driver,
    previous_signature: str | None = None,
    timeout: float = 12.0,
) -> None:
    """Wait for Majestic's asynchronous filter refresh, not merely for the old table."""

    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last_signature: str | None = None
    stable_polls = 0
    while time.monotonic() < deadline:
        signature = _backlinks_table_signature(driver)
        if signature == last_signature:
            stable_polls += 1
        else:
            last_signature = signature
            stable_polls = 0
        changed = previous_signature is None or signature != previous_signature
        elapsed = time.monotonic() - started
        # A changed table may settle quickly. If every result is already
        # DoFollow, no change is possible, so use a longer stability fallback.
        if signature and stable_polls >= 3 and (changed or elapsed >= 4.0):
            return
        time.sleep(0.4)
    raise MajesticReportError("Majestic DoFollow table did not settle after filtering")


def enable_dofollow_filter(driver) -> None:
    """Select the visible DoFollow filter; fail instead of silently using unfiltered data."""

    if _dofollow_is_active(driver):
        _wait_for_backlinks_settle(driver)
        return

    previous_signature = _backlinks_table_signature(driver)

    trigger_xpaths = (
        "//div[contains(@class,'mj-dropdown-trigger')][contains(normalize-space(.),'Follow')]",
        "//*[self::button or self::a][contains(@class,'filter') and contains(normalize-space(.),'Follow')]",
        "//*[self::button or self::a or self::div][normalize-space(.)='Follow']",
    )
    trigger = None
    for xpath in trigger_xpaths:
        candidates = list(_visible(driver.find_elements(By.XPATH, xpath)))
        if candidates:
            trigger = candidates[0]
            break
    if trigger is None:
        raise MajesticReportError("Majestic DoFollow filter trigger was not found")

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
    try:
        trigger.click()
    except Exception:
        driver.execute_script("arguments[0].click();", trigger)

    options = WebDriverWait(driver, 7).until(
        lambda drv: list(
            _visible(
                drv.find_elements(
                    By.XPATH,
                    "//*[self::a or self::button or self::label][contains(translate(normalize-space(.),'DF','df'),'dofollow')]",
                )
            )
        )
    )
    option = options[0]
    try:
        option.click()
    except Exception:
        driver.execute_script("arguments[0].click();", option)
    try:
        WebDriverWait(driver, 10).until(_dofollow_is_active)
    except TimeoutException as exc:
        raise MajesticReportError("Majestic did not confirm the active DoFollow filter") from exc
    _wait_for_backlinks_settle(driver, previous_signature=previous_signature)


def verify_backlinks_setup(driver) -> None:
    """Verify the deterministic report settings before extraction."""

    verify_index_source(driver, "F")
    try:
        query = parse_qs(urlparse(driver.current_url).query)
    except Exception as exc:
        raise MajesticReportError("Could not verify Majestic Backlinks URL") from exc
    if query.get("MaxSourceUrlsPerRefDomain", [""])[0] != "1":
        raise MajesticReportError("Majestic Backlinks is not set to 1 backlink per domain")
    if query.get("removeDeleted", ["0"])[0] == "1":
        raise MajesticReportError("Majestic Backlinks is hiding deleted backlinks")
    enable_dofollow_filter(driver)


BACKLINKS_JS = r"""
const text = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
const attr = (el, name) => el ? (el.getAttribute(name) || '') : '';
const visibleUrl = (box) => {
  const link = box && box.querySelector('a.redirectLink');
  const label = text(link) || text(box);
  const match = label.match(/https?:\/\/[^\s]+/i);
  return match ? match[0] : attr(link, 'href');
};
return Array.from(document.querySelectorAll('#vue-backlinks-table tbody > tr.odd, #vue-backlinks-table tbody > tr.even'))
  .filter(row => !row.classList.contains('js-copy-ignore'))
  .map(row => {
    const source = row.querySelector('td.backlink-source');
    const target = row.querySelector('td.a-backlink');
    const metrics = Array.from(row.querySelectorAll('td.flowMetric'));
    const outbound = row.querySelector('td[data-format-outbound-data="1"]');
    const outboundSpans = outbound ? Array.from(outbound.querySelectorAll('span')) : [];
    const outboundNumbers = outboundSpans.filter(x => /\d/.test(text(x)));
    const dateCell = row.querySelector('td.dateCell');
    const timeline = dateCell ? dateCell.querySelector('[data-dates]') : null;
    return {
      rank: text(row.querySelector('td.center')),
      source_url: visibleUrl(source && source.querySelector('.sourceURL')),
      source_url_href: attr(source && source.querySelector('.sourceURL a.redirectLink'), 'href'),
      source_url_display: text(source && source.querySelector('.sourceURL')),
      source_title: text(source && source.querySelector('.sourceTitle')),
      source_topic: text(source && source.querySelector('.topTopic .backlinkTitle')),
      language: text(source && source.querySelector('.linkType.lang .language')),
      anchor: text(source && source.querySelector('.anchorText')),
      source_url_tf: text(row.querySelector('.sourceURLTF')),
      source_url_cf: text(row.querySelector('.sourceURLCF')),
      source_domain_tf: text(metrics[2]),
      source_domain_cf: text(metrics[3]),
      outbound_total: text(outboundSpans.find(x => /total/i.test(attr(x, 'title'))) || outboundNumbers[0] || outboundSpans[0]),
      outbound_external: text(outboundSpans.find(x => /external/i.test(attr(x, 'title'))) || outboundNumbers[outboundNumbers.length - 1] || outboundSpans[outboundSpans.length - 1]),
      external_domains: text(metrics[4]),
      target_url: visibleUrl(target && target.querySelector('.targetURL')),
      target_url_href: attr(target && target.querySelector('.targetURL a.redirectLink'), 'href'),
      target_url_display: text(target && target.querySelector('.targetURL')),
      target_title: text(target && target.querySelector('.targetTitle')),
      target_topic: text(target && target.querySelector('.topTopic .backlinkTitle')),
      first_indexed: text(dateCell && dateCell.querySelector('.firstDate')),
      last_seen: text(dateCell && dateCell.querySelector('.lastDate')),
      deleted_date: text(dateCell && dateCell.querySelector('.deletedDate')),
      timeline_dates: attr(timeline, 'data-dates'),
      nofollow: !!(source && source.querySelector('.no-follow, [title*="NoFollow" i], [title*="nofollow" i]')),
      deleted: row.classList.contains('deleted-backlink') || !!(source && source.querySelector('.deleted')),
      sponsored: !!(source && source.querySelector('.sponsored, [title*="Sponsored" i]')),
      row_text: text(row)
    };
  });
"""


ANCHORS_JS = r"""
const text = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
return Array.from(document.querySelectorAll('table.resultstable.mj-table tbody > tr.odd, table.resultstable.mj-table tbody > tr.even'))
  .filter(row => !row.classList.contains('js-copy-ignore'))
  .map(row => {
    const cells = Array.from(row.children).filter(x => x.tagName === 'TD');
    return {
      rank: text(cells[0]),
      anchor: text(cells[1]),
      topic: text(cells[2]),
      referring_domains: text(cells[3]),
      total_links: text(cells[4]),
      deleted_links: text(cells[5]),
      nofollow_links: text(cells[6]),
      trust_flow: text(cells[7]),
      citation_flow: text(cells[8])
    };
  });
"""


PAGES_JS = r"""
const text = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
const attr = (el, name) => el ? (el.getAttribute(name) || '') : '';
const visibleUrl = (box) => {
  const link = box && box.querySelector('a.redirectLink');
  const label = text(link) || text(box);
  const match = label.match(/https?:\/\/[^\s]+/i);
  return match ? match[0] : attr(link, 'href');
};
return Array.from(document.querySelectorAll('#vue-pages-table tbody > tr.odd, #vue-pages-table tbody > tr.even'))
  .filter(row => !row.classList.contains('js-copy-ignore'))
  .map(row => {
    const cells = Array.from(row.children).filter(x => x.tagName === 'TD');
    const page = row.querySelector('td.backlink-source');
    const metrics = Array.from(row.querySelectorAll('td.flowMetric'));
    const outbound = row.querySelector('td[data-format-outbound-data="1"]');
    const outboundSpans = outbound ? Array.from(outbound.querySelectorAll('span')) : [];
    const outboundNumbers = outboundSpans.filter(x => /\d/.test(text(x)));
    return {
      rank: text(row.querySelector('.table-col-number')) || text(cells[0]),
      page_title: text(page && page.querySelector('.sourceTitle .backlinkTitle')),
      page_url: visibleUrl(page && page.querySelector('.sourceURL')),
      page_url_href: attr(page && page.querySelector('.sourceURL a.redirectLink'), 'href'),
      crawl_result: text(page && page.querySelector('.crawlResult')),
      language: text(page && page.querySelector('.linkType')),
      redirect_url: visibleUrl(page && page.querySelector('.redirectURL')),
      referring_urls: text(cells[2]),
      inbound_links: text(cells[3]),
      referring_domains: text(cells[4]),
      url_tf: text(metrics[0]),
      url_cf: text(metrics[1]),
      outbound_total: text(outboundSpans.find(x => /total/i.test(attr(x, 'title'))) || outboundNumbers[0] || outboundSpans[0]),
      outbound_external: text(outboundSpans.find(x => /external/i.test(attr(x, 'title'))) || outboundNumbers[outboundNumbers.length - 1] || outboundSpans[outboundSpans.length - 1]),
      external_domains: text(metrics[2]),
      last_seen: text(row.querySelector('td[data-split="1"] .aDate, .aDate')),
      row_text: text(row)
    };
  });
"""


def _collect_paginated(
    driver,
    url_builder: Callable[[int], str],
    table_selector: str,
    script: str,
    max_rows: int,
    before_extract: Callable[[], None] | None = None,
) -> Dict[str, Any]:
    collected: List[Dict[str, Any]] = []
    offset = 0
    truncated = False

    while len(collected) < max_rows:
        driver.get(url_builder(offset))
        _wait_for_report(driver, table_selector)
        if before_extract is not None:
            before_extract()
            _wait_for_report(driver, table_selector)

        page_rows = driver.execute_script(script) or []
        if not page_rows:
            break
        remaining = max_rows - len(collected)
        collected.extend(page_rows[:remaining])
        if len(page_rows) > remaining:
            truncated = True
            break
        if len(page_rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if len(collected) >= max_rows:
        truncated = True
    return {"rows": collected, "truncated": truncated, "pages": (offset // PAGE_SIZE) + 1 if collected else 0}


def collect_backlinks(driver, domain: str, max_rows: int = 200) -> Dict[str, Any]:
    result = _collect_paginated(
        driver=driver,
        url_builder=lambda offset: build_backlinks_url(domain, offset),
        table_selector="#vue-backlinks-table",
        script=BACKLINKS_JS,
        max_rows=max_rows,
        before_extract=lambda: verify_backlinks_setup(driver),
    )
    cleaned: List[Dict[str, Any]] = []
    for raw in result["rows"]:
        if raw.get("nofollow"):
            continue
        row = {key: _clean_text(value) if isinstance(value, str) else value for key, value in raw.items()}
        row["source_domain"] = _source_domain(row.get("source_url", ""))
        for key in (
            "rank",
            "source_url_tf",
            "source_url_cf",
            "source_domain_tf",
            "source_domain_cf",
            "outbound_total",
            "outbound_external",
            "external_domains",
        ):
            row[key] = _number(row.get(key))
        cleaned.append(row)
    result["rows"] = cleaned
    result["index_source"] = "F"
    result["filters"] = {"follow": "DoFollow", "per_ref_domain": "1", "deleted": "Included"}
    return result


def collect_anchor_text(driver, domain: str, index_source: str, max_rows: int = 500) -> Dict[str, Any]:
    source = index_source.upper()
    result = _collect_paginated(
        driver=driver,
        url_builder=lambda offset: build_anchor_url(domain, source, offset),
        table_selector="table.resultstable.mj-table",
        script=ANCHORS_JS,
        max_rows=max_rows,
        before_extract=lambda: verify_index_source(driver, source),
    )
    cleaned: List[Dict[str, Any]] = []
    for raw in result["rows"]:
        row = {key: _clean_text(value) if isinstance(value, str) else value for key, value in raw.items()}
        for key in (
            "rank",
            "referring_domains",
            "total_links",
            "deleted_links",
            "nofollow_links",
            "trust_flow",
            "citation_flow",
        ):
            row[key] = _number(row.get(key))
        row["index_source"] = source
        cleaned.append(row)
    result["rows"] = cleaned
    result["index_source"] = source
    return result


def collect_pages(driver, domain: str, index_source: str = "H", max_rows: int = 50) -> Dict[str, Any]:
    source = index_source.upper()
    result = _collect_paginated(
        driver=driver,
        url_builder=lambda offset: build_pages_url(domain, source, offset),
        table_selector="#vue-pages-table",
        script=PAGES_JS,
        max_rows=max_rows,
        before_extract=lambda: verify_index_source(driver, source),
    )
    cleaned: List[Dict[str, Any]] = []
    for raw in result["rows"]:
        row = {key: _clean_text(value) if isinstance(value, str) else value for key, value in raw.items()}
        for key in (
            "rank",
            "referring_urls",
            "inbound_links",
            "referring_domains",
            "url_tf",
            "url_cf",
            "outbound_total",
            "outbound_external",
            "external_domains",
        ):
            row[key] = _number(row.get(key))
        row["index_source"] = source
        cleaned.append(row)
    result["rows"] = cleaned
    result["index_source"] = source
    return result
