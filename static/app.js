const els = {
  titleInput: document.getElementById("titleInput"),
  domainsInput: document.getElementById("domainsInput"),
  addBatchBtn: document.getElementById("addBatchBtn"),
  clearBatchBtn: document.getElementById("clearBatchBtn"),
  startBtn: document.getElementById("startBtn"),
  pauseBtn: document.getElementById("pauseBtn"),
  stopBtn: document.getElementById("stopBtn"),
  loginBtn: document.getElementById("loginBtn"),
  browserReady: document.getElementById("browserReady"),
  lastStatus: document.getElementById("lastStatus"),
  currentTitle: document.getElementById("currentTitle"),
  currentDomain: document.getElementById("currentDomain"),
  countTotal: document.getElementById("countTotal"),
  countProcessed: document.getElementById("countProcessed"),
  countRemaining: document.getElementById("countRemaining"),
  countGood: document.getElementById("countGood"),
  countNear: document.getElementById("countNear"),
  countBad: document.getElementById("countBad"),
  countNotFound: document.getElementById("countNotFound"),
  countErrors: document.getElementById("countErrors"),
  countOld: document.getElementById("countOld"),
  sheetsReady: document.getElementById("sheetsReady"),
  openaiReady: document.getElementById("openaiReady"),
  duplicateDbSize: document.getElementById("duplicateDbSize"),
  loadStats: document.getElementById("loadStats"),
  queueMeta: document.getElementById("queueMeta"),
  queueGroupsBox: document.getElementById("queueGroupsBox"),
  browserErrorBox: document.getElementById("browserErrorBox"),
  sheetsErrorBox: document.getElementById("sheetsErrorBox"),
  openaiErrorBox: document.getElementById("openaiErrorBox"),
  openaiNoticeBox: document.getElementById("openaiNoticeBox"),
  logBox: document.getElementById("logBox"),
  clearLogBtn: document.getElementById("clearLogBtn"),
  resultsBody: document.getElementById("resultsBody"),
  batchDomainsTemplate: document.getElementById("batchDomainsTemplate"),
  promptEditorBtn: document.getElementById("promptEditorBtn"),
  strictModeToggle: document.getElementById("strictModeToggle"),
  strictUniqueDeficitInput: document.getElementById("strictUniqueDeficitInput"),
  strictArticleDeficitInput: document.getElementById("strictArticleDeficitInput"),
  freshnessFilterToggle: document.getElementById("freshnessFilterToggle"),
  freshnessCutoffYearInput: document.getElementById("freshnessCutoffYearInput"),
  freshnessMaxOldShareInput: document.getElementById("freshnessMaxOldShareInput"),
  qualityModelSelect: document.getElementById("qualityModelSelect"),
  promptDialog: document.getElementById("promptDialog"),
  closePromptBtn: document.getElementById("closePromptBtn"),
  screenPromptInput: document.getElementById("screenPromptInput"),
  linkPromptInput: document.getElementById("linkPromptInput"),
  articlePromptInput: document.getElementById("articlePromptInput"),
  anchorPromptInput: document.getElementById("anchorPromptInput"),
  promptUpdatedAt: document.getElementById("promptUpdatedAt"),
  reloadPromptsBtn: document.getElementById("reloadPromptsBtn"),
  savePromptsBtn: document.getElementById("savePromptsBtn"),
  duplicatesRemoveInput: document.getElementById("duplicatesRemoveInput"),
  removeDuplicatesBtn: document.getElementById("removeDuplicatesBtn"),
  removeAndReuseBtn: document.getElementById("removeAndReuseBtn"),
  duplicatesRemoveResult: document.getElementById("duplicatesRemoveResult"),
};

let lastLogsText = "";
let lastQueueSignature = "";
let lastResultsSignature = "";
let startInFlight = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function statusClass(status) {
  const value = String(status || "");
  if (value === "GOOD") return "status-good";
  if (value.startsWith("BAD") || value.startsWith("ERROR")) return "status-bad";
  if (value.startsWith("PENDING") || value.startsWith("RETRY")) return "status-mid";
  if (value.includes("GOOD")) return "status-mid";
  return "";
}

function compactMetric(row) {
  return compactMetricParts(row).filter(Boolean).join(" ");
}

function compactMetricParts(row) {
  const articles = Number(row.article_links) < 0 ? "AI" : (row.article_links || 0);
  const main = `U${row.unique_quality || 0} A${articles} H${row.homepage_links || 0}`;
  const yearMin = Number(row.link_year_min || 0);
  const yearMax = Number(row.link_year_max || 0);
  const yearCount = Number(row.link_year_count || 0);
  const uniqueQuality = Number(row.unique_quality || 0);
  let years = "";
  if (yearMin > 0 && yearMax > 0) {
    years = yearMin === yearMax ? `Y${yearMin}` : `Y${yearMin}-${yearMax}`;
    if (yearCount > 0 && uniqueQuality > 0 && yearCount < uniqueQuality) {
      years = `${years} ${yearCount}/${uniqueQuality}`;
    }
  }
  return [main, years];
}

function renderCompactMetric(row) {
  const [main, years] = compactMetricParts(row);
  if (!years) return escapeHtml(main || "");
  return `${escapeHtml(main || "")}<span class="metric-years">${escapeHtml(years)}</span>`;
}

function isPendingRow(row) {
  const status = String(row?.status || "");
  return status.startsWith("PENDING") || status.startsWith("RETRY");
}

function isLoadingStatus(value) {
  const status = String(value || "").toUpperCase();
  return status.includes("CHECKING") || status.includes("QUEUED") || status.startsWith("PENDING");
}

function compactCodeLabel(value) {
  return String(value || "")
    .replace(/^(BAD|ERROR|SKIP|PENDING|RETRY):/i, "")
    .replaceAll("_", " ")
    .trim()
    .toUpperCase();
}

function displayOutcomeStatus(value) {
  const status = String(value || "");
  const labels = {
    "GOOD": "GOOD",
    "GOOD (NEAR THRESHOLD)": "GOOD NEAR",
    "BAD": "BAD",
    "Not found": "NOT FOUND",
    "PENDING:AI": "AI CHECKING",
    "PENDING:WEBARCHIVE": "ARCHIVE CHECKING",
    "BAD:LOW_PROFILE": "LOW PROFILE",
    "BAD:HOMEPAGE_SHARE": "LOW HOME",
    "BAD:CONTEXT_DENSITY": "CTX DENSITY",
    "BAD:CONTEXT_OUTBOUND": "CTX OUTBOUND",
    "BAD:CONTEXT_HOMEPAGE_SHARE": "CTX HOME",
    "BAD:AI_HARD_STOP": "HARD STOP",
    "BAD:AI_RISK": "AI RISK",
    "BAD:WEBARCHIVE_SPAM": "ARCHIVE SPAM",
    "BAD:DOMAIN_NAME": "BAD NAME",
    "BAD:HISTORIC_PAGES": "USED BEFORE",
    "ERROR:AI": "AI ERROR",
    "ERROR:WEBARCHIVE_TIMEOUT": "ARCHIVE TIMEOUT",
    "ERROR:WEBARCHIVE_FETCH": "ARCHIVE FETCH ERROR",
    "ERROR:WEBARCHIVE_CDX": "ARCHIVE CDX ERROR",
    "ERROR:RETRY_LIMIT": "RETRY LIMIT",
    "ERROR:MAJESTIC_REPORT": "MAJESTIC ERROR",
    "ERROR:BROWSER_DEAD": "BROWSER DEAD",
    "ERROR:AI_NOT_CONFIGURED": "AI NOT READY",
  };
  if (labels[status]) return labels[status];
  if (!status) return "-";
  return compactCodeLabel(status) || status.toUpperCase();
}

function displayAiStatus(value) {
  const status = String(value || "");
  if (!status || status === "-") return "-";
  if (/^OK\s+\d+$/i.test(status)) return status.replace(/\s+/, " · ");
  if (status === "SKIP:LOCAL") return "LOCAL SKIP";
  if (status === "SKIP:CONTEXT") return "CONTEXT";
  if (status === "QUEUED") return "QUEUED";
  if (status === "CHECKING") return "CHECKING";
  if (status === "ERROR") return "ERROR";
  return compactCodeLabel(status) || status.toUpperCase();
}

function displayWebarchiveStatus(value) {
  const status = String(value || "");
  if (!status || status === "-") return "-";
  if (status === "—") return "—";
  if (/^OK\s+\d+$/i.test(status)) return status.replace(/\s+/, " · ");
  if (/^SPAM\s+\d+$/i.test(status)) return status.replace(/\s+/, " · ");
  if (/^QUEUED\s+RETRY\s+\d+$/i.test(status)) return status.replace(/^QUEUED\s+RETRY\s+/i, "RETRY · ");
  if (/^CHECKING\s+RETRY\s+\d+$/i.test(status)) return status.replace(/^CHECKING\s+RETRY\s+/i, "RETRY · ");
  if (status === "QUEUED") return "QUEUED";
  if (status === "CHECKING") return "CHECKING";
  if (status === "OFF") return "OFF";
  if (status === "SKIP:NO_HTML") return "NO HTML";
  if (status === "SKIP:CDX_TIMEOUT") return "CDX TIMEOUT";
  if (status === "SKIP:FETCH_TIMEOUT") return "FETCH TIMEOUT";
  if (status === "SKIP:FETCH_ERROR") return "FETCH ERROR";
  if (status === "SKIP:CDX_ERROR") return "CDX ERROR";
  return compactCodeLabel(status) || status.toUpperCase();
}

function renderStatusValue(value, label = value) {
  const raw = String(value || "-");
  const text = String(label || raw || "-");
  if (!isLoadingStatus(raw) && !isLoadingStatus(text)) return escapeHtml(text);
  return `<span class="loading-status"><span class="loading-dot"></span>${escapeHtml(text)}</span>`;
}

function cleanDeficit(value, fallback = 1) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(0, Math.min(parsed, 50));
}

function cleanYear(value, fallback = 2016) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1990, Math.min(parsed, 2030));
}

function cleanPercent(value, fallback = 50) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(0, Math.min(parsed, 100));
}

async function requestJSON(url, options = {}) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function postJSON(url, body = {}) {
  return requestJSON(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function deleteJSON(url) {
  return requestJSON(url, { method: "DELETE" });
}

function renderResults(rows) {
  const signature = JSON.stringify(rows);
  if (signature === lastResultsSignature) return;
  lastResultsSignature = signature;
  els.resultsBody.innerHTML = "";
  const orderedRows = rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const pendingDelta = Number(isPendingRow(b.row)) - Number(isPendingRow(a.row));
      if (pendingDelta) return pendingDelta;
      return b.index - a.index;
    })
    .map((item) => item.row);
  orderedRows.forEach((row) => {
    const reason = row.ai_reason || "";
    const error = row.error || "";
    const reasonText = reason || error;
    const metric = compactMetric(row);
    const aiStatus = row.ai_status || "-";
    const locale = [row.locale, row.locale_source].filter(Boolean).join(" · ");
    const localeDetails = [
      row.locale_evidence,
      row.locale_confidence ? `Уверенность: ${row.locale_confidence}` : "",
      (row.locale_evidence || row.locale_confidence || row.country_origin)
        ? (row.locale_market_confirmed ? "Рынок подтверждён прямыми сигналами" : "Рынок не подтверждён прямыми сигналами")
        : "",
      row.country_origin ? `Страна происхождения: ${row.country_origin}` : "",
      row.locale_archive_snapshot ? `WebArchive: ${row.locale_archive_snapshot}` : "",
    ].filter(Boolean).join(" | ");
    const webarchiveStatus = row.webarchive_status || "—";
    const pinned = isPendingRow(row);
    const outcomeLabel = displayOutcomeStatus(row.status || "");
    const aiLabel = displayAiStatus(aiStatus);
    const webarchiveLabel = displayWebarchiveStatus(webarchiveStatus);
    const tr = document.createElement("tr");
    tr.className = pinned ? "result-row-pinned" : "";
    tr.innerHTML = `
      <td>${escapeHtml(row.title || "")}</td>
      <td><strong>${escapeHtml(row.domain || "")}</strong></td>
      <td class="${statusClass(row.status)}" title="${escapeHtml(row.status || "")}">${renderStatusValue(row.status || "", outcomeLabel)}</td>
      <td title="${escapeHtml(localeDetails)}">${escapeHtml(locale)}</td>
      <td title="${escapeHtml(aiStatus)}">${renderStatusValue(aiStatus, aiLabel)}</td>
      <td class="metric-cell" title="${escapeHtml(metric)}">${renderCompactMetric(row)}</td>
      <td title="${escapeHtml(webarchiveStatus)}">${renderStatusValue(webarchiveStatus, webarchiveLabel)}</td>
      <td class="reason-cell" title="${escapeHtml(reasonText)}">${escapeHtml(reasonText)}</td>
    `;
    els.resultsBody.appendChild(tr);
  });
}

function attachAddDomainsBox(card, batch) {
  const existing = card.querySelector(".batch-domains-box");
  if (existing) {
    existing.remove();
    return;
  }
  const box = els.batchDomainsTemplate.content.firstElementChild.cloneNode(true);
  card.appendChild(box);
  box.querySelector(".batch-domains-title").textContent = `Добавить домены в «${batch.title}»`;
  box.querySelector(".batch-domains-input").focus();
  box.querySelector(".cancel-batch-domains").addEventListener("click", () => box.remove());
  box.querySelector(".confirm-batch-domains").addEventListener("click", async () => {
    const domains = box.querySelector(".batch-domains-input").value.trim();
    if (!domains) return alert("Вставь хотя бы один домен");
    try {
      const data = await postJSON(`/api/batches/${batch.batch_id}/add-domains`, { domains });
      const summary = `Добавлено ${data.loaded || 0} · дубли ${data.duplicates_skipped || 0} · из txt ${data.duplicates_from_file || 0} · невалидных ${data.invalid_skipped || 0}`;
      if ((data.loaded || 0) > 0) {
        box.remove();
      } else {
        box.querySelector(".batch-domains-result").textContent = summary;
      }
      lastQueueSignature = "";
      await refreshStatus();
    } catch (error) { alert(error.message); }
  });
}

function renderQueueBatches(batches) {
  const signature = JSON.stringify(batches);
  if (signature === lastQueueSignature) return;
  lastQueueSignature = signature;
  els.queueGroupsBox.innerHTML = "";
  if (!batches.length) {
    els.queueGroupsBox.innerHTML = '<div class="queue-empty">Очередь пустая — добавьте первую пачку слева</div>';
    return;
  }

  batches.forEach((batch, index) => {
    const card = document.createElement("div");
    card.className = "queue-group-card";
    card.innerHTML = `
      <div class="queue-group-head">
        <div class="queue-summary">
          <span class="queue-position">${index + 1}</span>
          <div class="queue-summary-text">
            <strong>${escapeHtml(batch.title)}</strong>
            <span>${batch.queued} в очереди · ${batch.processing} в работе · ${batch.done} готово · всего ${batch.total}</span>
          </div>
        </div>
        <div class="queue-actions">
          <div class="move-controls" aria-label="Изменить порядок пачки">
            <button class="small secondary move-up-btn" title="Поднять пачку" ${index === 0 ? "disabled" : ""}>↑</button>
            <button class="small secondary move-down-btn" title="Опустить пачку" ${index === batches.length - 1 ? "disabled" : ""}>↓</button>
          </div>
          <button class="small secondary add-domains-btn" title="Добавить домены в эту пачку">Добавить</button>
          <button class="small danger remove-batch-btn">Удалить</button>
        </div>
      </div>
    `;

    const move = async (direction) => {
      try {
        await postJSON(`/api/batches/${batch.batch_id}/move`, { direction });
        lastQueueSignature = "";
        await refreshStatus();
      } catch (error) { alert(error.message); }
    };
    card.querySelector(".move-up-btn").addEventListener("click", () => move("up"));
    card.querySelector(".move-down-btn").addEventListener("click", () => move("down"));
    card.querySelector(".remove-batch-btn").addEventListener("click", async () => {
      if (!confirm(`Удалить оставшиеся домены пачки «${batch.title}»?`)) return;
      try {
        await deleteJSON(`/api/batches/${batch.batch_id}`);
        lastQueueSignature = "";
        await refreshStatus();
      } catch (error) { alert(error.message); }
    });
    card.querySelector(".add-domains-btn").addEventListener("click", () => attachAddDomainsBox(card, batch));
    els.queueGroupsBox.appendChild(card);
  });
}

function showError(box, prefix, value) {
  if (value) {
    box.textContent = prefix ? `${prefix}: ${value}` : value;
    box.classList.remove("hidden");
  } else {
    box.classList.add("hidden");
  }
}

function updateStatus(data) {
  document.body.classList.toggle("login-required", Boolean(data.login_required));
  els.browserReady.textContent = data.browser_ready ? "Готов" : (data.browser_launching ? "Запускается" : "Не готов");
  els.lastStatus.textContent = data.last_status || "—";
  els.currentTitle.textContent = data.current_title || "Без активной пачки";
  els.currentDomain.textContent = data.current_domain || "Ожидание запуска";

  const counts = data.counts || {};
  els.countTotal.textContent = counts.total || 0;
  els.countProcessed.textContent = counts.processed || 0;
  els.countRemaining.textContent = counts.remaining || 0;
  els.countGood.textContent = counts.good || 0;
  els.countNear.textContent = counts.near || 0;
  els.countBad.textContent = counts.bad || 0;
  els.countNotFound.textContent = counts.not_found || 0;
  els.countErrors.textContent = counts.errors || 0;
  els.countOld.textContent = counts.old || 0;
  els.sheetsReady.textContent = data.sheets_ready ? "ОК" : "Не готов";
  if (data.openai_ready) {
    let provider = "OpenAI";
    try { provider = new URL(data.openai_base_url).hostname; } catch (_) {}
    const cascade = data.openai_screen_enabled && data.openai_screen_model && data.openai_screen_model !== data.openai_model
      ? `${data.openai_screen_model} → ${data.openai_model}`
      : data.openai_model;
    els.openaiReady.textContent = `${cascade} · ${provider}`;
  } else {
    els.openaiReady.textContent = "Не готов";
  }
  els.duplicateDbSize.textContent = `${Number(data.duplicate_db_size || 0).toLocaleString("ru-RU")} дублей`;

  const stats = data.load_stats || {};
  els.loadStats.textContent = `Добавлено ${stats.loaded || 0} · дублей в пачке ${stats.duplicates_skipped || 0} · уже проверенных ${stats.duplicates_from_file || 0} · невалидных ${stats.invalid_skipped || 0}`;

  const batches = data.queue_batches || [];
  const activeTotal = batches.reduce((sum, batch) => sum + (batch.queued || 0) + (batch.processing || 0), 0);
  els.queueMeta.textContent = `${batches.length} пачек · ${activeTotal} доменов`;
  renderQueueBatches(batches);

  showError(els.browserErrorBox, "Chrome", data.browser_error);
  showError(els.sheetsErrorBox, "Google Sheets", data.sheets_error);
  showError(els.openaiErrorBox, "OpenAI", data.openai_error);
  showError(els.openaiNoticeBox, "OpenAI", data.openai_notice);

  const logsText = (data.logs || []).join("\n");
  if (logsText !== lastLogsText) {
    els.logBox.textContent = logsText;
    els.logBox.scrollTop = els.logBox.scrollHeight;
    lastLogsText = logsText;
  }
  renderResults(data.results || []);
  if (els.strictModeToggle && els.strictModeToggle.checked !== Boolean(data.strict_mode)) {
    els.strictModeToggle.checked = Boolean(data.strict_mode);
  }
  if (
    els.strictUniqueDeficitInput
    && document.activeElement !== els.strictUniqueDeficitInput
    && Number(els.strictUniqueDeficitInput.value) !== Number(data.strict_unique_deficit ?? 1)
  ) {
    els.strictUniqueDeficitInput.value = cleanDeficit(data.strict_unique_deficit, 1);
  }
  if (
    els.strictArticleDeficitInput
    && document.activeElement !== els.strictArticleDeficitInput
    && Number(els.strictArticleDeficitInput.value) !== Number(data.strict_article_deficit ?? 1)
  ) {
    els.strictArticleDeficitInput.value = cleanDeficit(data.strict_article_deficit, 1);
  }
  if (els.freshnessFilterToggle && els.freshnessFilterToggle.checked !== Boolean(data.freshness_filter_enabled)) {
    els.freshnessFilterToggle.checked = Boolean(data.freshness_filter_enabled);
  }
  if (
    els.freshnessCutoffYearInput
    && document.activeElement !== els.freshnessCutoffYearInput
    && Number(els.freshnessCutoffYearInput.value) !== Number(data.freshness_cutoff_year ?? 2016)
  ) {
    els.freshnessCutoffYearInput.value = cleanYear(data.freshness_cutoff_year, 2016);
  }
  if (
    els.freshnessMaxOldShareInput
    && document.activeElement !== els.freshnessMaxOldShareInput
    && Number(els.freshnessMaxOldShareInput.value) !== Number(data.freshness_max_old_share_percent ?? 50)
  ) {
    els.freshnessMaxOldShareInput.value = cleanPercent(data.freshness_max_old_share_percent, 50);
  }
  if (els.qualityModelSelect) {
    const choices = Array.isArray(data.quality_model_choices) && data.quality_model_choices.length
      ? data.quality_model_choices
      : ["gpt-5.6-sol", "gpt-5.6-terra"];
    const expectedOptions = choices.map((model) => `${model}:${model}`).join("|");
    const currentOptions = [...els.qualityModelSelect.options]
      .map((option) => `${option.value}:${option.textContent}`)
      .join("|");
    if (currentOptions !== expectedOptions) {
      els.qualityModelSelect.innerHTML = choices.map((model) => {
        return `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`;
      }).join("");
    }
    if (data.quality_model && els.qualityModelSelect.value !== data.quality_model) {
      els.qualityModelSelect.value = data.quality_model;
    }
  }
  els.pauseBtn.textContent = data.paused ? "Продолжить" : "Пауза";
  els.pauseBtn.disabled = !data.running;
  els.stopBtn.disabled = !data.running && !data.login_required && !data.current_domain;
  els.startBtn.disabled = startInFlight || data.running || data.browser_recovery_in_progress || !data.browser_ready || counts.remaining === 0;
}

async function refreshStatus() {
  try { updateStatus(await requestJSON("/api/status")); }
  catch (error) { console.error(error); }
}

async function loadPrompts() {
  els.reloadPromptsBtn.disabled = true;
  try {
    const data = await requestJSON("/api/prompts");
    els.screenPromptInput.value = data.screen_prompt || "";
    els.linkPromptInput.value = data.link_prompt || "";
    els.articlePromptInput.value = data.article_prompt || "";
    els.anchorPromptInput.value = data.anchor_prompt || "";
    els.promptUpdatedAt.textContent = data.updated_at ? `Сохранено: ${data.updated_at}` : "—";
    return true;
  } catch (error) {
    alert(error.message);
    return false;
  }
  finally { els.reloadPromptsBtn.disabled = false; }
}

document.querySelectorAll("[data-prompt-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.promptTab;
    document.querySelectorAll("[data-prompt-tab]").forEach((tab) => tab.classList.toggle("active", tab === button));
    document.querySelectorAll("[data-prompt-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.promptPanel === target));
  });
});

els.promptEditorBtn.addEventListener("click", async () => {
  if (await loadPrompts()) els.promptDialog.showModal();
});
els.reloadPromptsBtn.addEventListener("click", loadPrompts);
els.savePromptsBtn.addEventListener("click", async () => {
  if (!els.screenPromptInput.value.trim() || !els.linkPromptInput.value.trim() || !els.articlePromptInput.value.trim() || !els.anchorPromptInput.value.trim()) return alert("Все четыре промпта должны быть заполнены");
  els.savePromptsBtn.disabled = true;
  els.savePromptsBtn.textContent = "Сохранение…";
  try {
    const data = await postJSON("/api/prompts", {
      screen_prompt: els.screenPromptInput.value,
      link_prompt: els.linkPromptInput.value,
      article_prompt: els.articlePromptInput.value,
      anchor_prompt: els.anchorPromptInput.value,
    });
    els.promptUpdatedAt.textContent = `Сохранено: ${data.updated_at}`;
    els.savePromptsBtn.textContent = "Сохранено";
    setTimeout(() => { els.savePromptsBtn.textContent = "Сохранить промпты"; }, 1200);
  } catch (error) {
    els.savePromptsBtn.textContent = "Сохранить промпты";
    alert(error.message);
  } finally { els.savePromptsBtn.disabled = false; }
});

function normalizedDomainLines(raw) {
  return [...new Set(String(raw || "").split(/\r?\n/).map((line) => {
    let value = line.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "");
    value = value.split(/[/?#]/, 1)[0];
    return value;
  }).filter(Boolean))];
}

async function removeDuplicatesForRecheck(reuse) {
  const domains = els.duplicatesRemoveInput.value.trim();
  const lines = normalizedDomainLines(domains);
  if (!lines.length) return alert("Вставь хотя бы один домен");
  if (!confirm(`Удалить из базы дублей доменов: ${lines.length}?`)) return;
  els.removeDuplicatesBtn.disabled = true;
  els.removeAndReuseBtn.disabled = true;
  try {
    const data = await postJSON("/api/duplicates/remove", { domains: lines.join("\n") });
    els.duplicatesRemoveResult.textContent = `Удалено: ${data.removed} · не найдено: ${data.not_found} · осталось в базе: ${data.remaining}`;
    if (reuse) {
      const existing = normalizedDomainLines(els.domainsInput.value);
      els.domainsInput.value = [...new Set([...existing, ...lines])].join("\n");
      els.domainsInput.focus();
    }
    els.duplicatesRemoveInput.value = "";
    await refreshStatus();
  } catch (error) { alert(error.message); }
  finally {
    els.removeDuplicatesBtn.disabled = false;
    els.removeAndReuseBtn.disabled = false;
  }
}

els.removeDuplicatesBtn.addEventListener("click", () => removeDuplicatesForRecheck(false));
els.removeAndReuseBtn.addEventListener("click", () => removeDuplicatesForRecheck(true));

els.addBatchBtn.addEventListener("click", async () => {
  const title = els.titleInput.value.trim();
  const domains = els.domainsInput.value.trim();
  if (!title) return alert("Заполни название или локаль пачки");
  if (!domains) return alert("Добавь хотя бы один домен");
  try {
    const data = await postJSON("/api/batches", { title, domains });
    els.domainsInput.value = "";
    if (!data.merged) els.titleInput.value = "";
    lastQueueSignature = "";
    await refreshStatus();
    (data.merged ? els.domainsInput : els.titleInput).focus();
  } catch (error) { alert(error.message); }
});

els.clearBatchBtn.addEventListener("click", () => {
  els.titleInput.value = "";
  els.domainsInput.value = "";
  els.titleInput.focus();
});

els.startBtn.addEventListener("click", async () => {
  if (startInFlight) return;
  startInFlight = true;
  els.startBtn.disabled = true;
  try { await postJSON("/api/start"); await refreshStatus(); } catch (error) { alert(error.message); }
  finally { startInFlight = false; }
});
els.pauseBtn.addEventListener("click", async () => {
  try { await postJSON("/api/pause"); await refreshStatus(); } catch (error) { alert(error.message); }
});
els.stopBtn.addEventListener("click", async () => {
  try { await postJSON("/api/stop"); await refreshStatus(); } catch (error) { alert(error.message); }
});
els.loginBtn.addEventListener("click", async () => {
  try { await postJSON("/api/login-confirm"); await refreshStatus(); } catch (error) { alert(error.message); }
});
els.clearLogBtn.addEventListener("click", async (event) => {
  event.preventDefault();
  event.stopPropagation();
  try {
    await postJSON("/api/logs/clear");
    els.logBox.textContent = "";
    lastLogsText = "";
  } catch (error) { alert(error.message); }
});

function strictSettingsPayload() {
  return {
    strict_mode: Boolean(els.strictModeToggle?.checked),
    strict_unique_deficit: cleanDeficit(els.strictUniqueDeficitInput?.value, 1),
    strict_article_deficit: cleanDeficit(els.strictArticleDeficitInput?.value, 1),
    freshness_filter_enabled: Boolean(els.freshnessFilterToggle?.checked),
    freshness_cutoff_year: cleanYear(els.freshnessCutoffYearInput?.value, 2016),
    freshness_max_old_share_percent: cleanPercent(els.freshnessMaxOldShareInput?.value, 50),
    quality_model: els.qualityModelSelect?.value || "gpt-5.6-sol",
  };
}

async function saveStrictSettings() {
  const payload = strictSettingsPayload();
  localStorage.setItem("majuiStrictMode", payload.strict_mode ? "1" : "0");
  localStorage.setItem("majuiStrictUniqueDeficit", String(payload.strict_unique_deficit));
  localStorage.setItem("majuiStrictArticleDeficit", String(payload.strict_article_deficit));
  localStorage.setItem("majuiFreshnessFilterEnabled", payload.freshness_filter_enabled ? "1" : "0");
  localStorage.setItem("majuiFreshnessCutoffYear", String(payload.freshness_cutoff_year));
  localStorage.setItem("majuiFreshnessMaxOldShare", String(payload.freshness_max_old_share_percent));
  localStorage.setItem("majuiQualityModel", payload.quality_model);
  if (els.strictUniqueDeficitInput) els.strictUniqueDeficitInput.value = payload.strict_unique_deficit;
  if (els.strictArticleDeficitInput) els.strictArticleDeficitInput.value = payload.strict_article_deficit;
  if (els.freshnessCutoffYearInput) els.freshnessCutoffYearInput.value = payload.freshness_cutoff_year;
  if (els.freshnessMaxOldShareInput) els.freshnessMaxOldShareInput.value = payload.freshness_max_old_share_percent;
  const data = await postJSON("/api/settings", payload);
  if (els.strictModeToggle) els.strictModeToggle.checked = Boolean(data.strict_mode);
  if (els.strictUniqueDeficitInput) els.strictUniqueDeficitInput.value = cleanDeficit(data.strict_unique_deficit, payload.strict_unique_deficit);
  if (els.strictArticleDeficitInput) els.strictArticleDeficitInput.value = cleanDeficit(data.strict_article_deficit, payload.strict_article_deficit);
  if (els.freshnessFilterToggle) els.freshnessFilterToggle.checked = Boolean(data.freshness_filter_enabled);
  if (els.freshnessCutoffYearInput) els.freshnessCutoffYearInput.value = cleanYear(data.freshness_cutoff_year, payload.freshness_cutoff_year);
  if (els.freshnessMaxOldShareInput) els.freshnessMaxOldShareInput.value = cleanPercent(data.freshness_max_old_share_percent, payload.freshness_max_old_share_percent);
  if (els.qualityModelSelect && data.quality_model) els.qualityModelSelect.value = data.quality_model;
}

if (els.strictModeToggle) {
  els.strictModeToggle.addEventListener("change", async () => {
    try { await saveStrictSettings(); await refreshStatus(); }
    catch (error) { alert(error.message); els.strictModeToggle.checked = !els.strictModeToggle.checked; }
  });
}
for (const input of [els.strictUniqueDeficitInput, els.strictArticleDeficitInput]) {
  if (!input) continue;
  input.addEventListener("change", async () => {
    try { await saveStrictSettings(); await refreshStatus(); }
    catch (error) { alert(error.message); }
  });
}
if (els.freshnessFilterToggle) {
  els.freshnessFilterToggle.addEventListener("change", async () => {
    try { await saveStrictSettings(); await refreshStatus(); }
    catch (error) { alert(error.message); els.freshnessFilterToggle.checked = !els.freshnessFilterToggle.checked; }
  });
}
for (const input of [els.freshnessCutoffYearInput, els.freshnessMaxOldShareInput]) {
  if (!input) continue;
  input.addEventListener("change", async () => {
    try { await saveStrictSettings(); await refreshStatus(); }
    catch (error) { alert(error.message); }
  });
}
if (els.qualityModelSelect) {
  els.qualityModelSelect.addEventListener("change", async () => {
    try { await saveStrictSettings(); await refreshStatus(); }
    catch (error) { alert(error.message); }
  });
}

async function initialize() {
  const savedStrictMode = localStorage.getItem("majuiStrictMode");
  const savedStrictUniqueDeficit = localStorage.getItem("majuiStrictUniqueDeficit");
  const savedStrictArticleDeficit = localStorage.getItem("majuiStrictArticleDeficit");
  const savedFreshnessFilterEnabled = localStorage.getItem("majuiFreshnessFilterEnabled");
  const savedFreshnessCutoffYear = localStorage.getItem("majuiFreshnessCutoffYear");
  const savedFreshnessMaxOldShare = localStorage.getItem("majuiFreshnessMaxOldShare");
  const savedQualityModel = localStorage.getItem("majuiQualityModel");
  if (els.strictModeToggle && savedStrictMode !== null) {
    els.strictModeToggle.checked = savedStrictMode === "1";
  }
  if (els.strictUniqueDeficitInput && savedStrictUniqueDeficit !== null) {
    els.strictUniqueDeficitInput.value = cleanDeficit(savedStrictUniqueDeficit, 1);
  }
  if (els.strictArticleDeficitInput && savedStrictArticleDeficit !== null) {
    els.strictArticleDeficitInput.value = cleanDeficit(savedStrictArticleDeficit, 1);
  }
  if (els.freshnessFilterToggle && savedFreshnessFilterEnabled !== null) {
    els.freshnessFilterToggle.checked = savedFreshnessFilterEnabled === "1";
  }
  if (els.freshnessCutoffYearInput && savedFreshnessCutoffYear !== null) {
    els.freshnessCutoffYearInput.value = cleanYear(savedFreshnessCutoffYear, 2016);
  }
  if (els.freshnessMaxOldShareInput && savedFreshnessMaxOldShare !== null) {
    els.freshnessMaxOldShareInput.value = cleanPercent(savedFreshnessMaxOldShare, 50);
  }
  if (els.qualityModelSelect && savedQualityModel !== null) {
    els.qualityModelSelect.value = savedQualityModel;
  }
  if (
    savedStrictMode !== null
    || savedStrictUniqueDeficit !== null
    || savedStrictArticleDeficit !== null
    || savedFreshnessFilterEnabled !== null
    || savedFreshnessCutoffYear !== null
    || savedFreshnessMaxOldShare !== null
    || savedQualityModel !== null
  ) {
    try { await saveStrictSettings(); } catch (error) { console.error(error); }
  }
  await refreshStatus();
  setInterval(refreshStatus, 1000);
}

initialize();
