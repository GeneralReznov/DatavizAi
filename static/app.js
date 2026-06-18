(function () {
  'use strict';

  const PAGE_SIZE = 25;
  let allResults     = [];
  let filteredResults = [];
  let currentPage    = 1;
  let searchTimer    = null;

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const inputCsv   = document.getElementById('input-csv');
  const inputExcel = document.getElementById('input-excel');
  const inputPdf   = document.getElementById('input-pdf');
  const processBtn = document.getElementById('process-btn');
  const processErr = document.getElementById('process-error');
  const generateBtn  = document.getElementById('generate-btn');
  const generateErr  = document.getElementById('generate-error');
  const statsSection   = document.getElementById('stats-section');
  const resultsSection = document.getElementById('results-section');
  const outputsSection = document.getElementById('outputs-section');
  const tableBody  = document.getElementById('table-body');
  const noResults  = document.getElementById('no-results');
  const searchInput  = document.getElementById('search-input');
  const filterBlock  = document.getElementById('filter-block');
  const filterCodes  = document.getElementById('filter-codes');
  const exportBtn    = document.getElementById('export-btn');
  const pagination   = document.getElementById('pagination');
  const loadingOverlay = document.getElementById('loading-overlay');
  const loadingText    = document.getElementById('loading-text');
  const resetBtn       = document.getElementById('reset-btn');

  // ── State tracking ────────────────────────────────────────────────────────
  const uploaded = { csv: false, excel: false, pdf: false };

  function updateButtons() {
    const canProcess  = uploaded.csv && uploaded.excel;
    processBtn.disabled  = !canProcess;
    generateBtn.disabled = !canProcess;
  }

  // ── Loading helpers ───────────────────────────────────────────────────────
  function showLoading(text) {
    loadingText.textContent = text || 'Processing…';
    loadingOverlay.style.display = 'flex';
  }
  function hideLoading() {
    loadingOverlay.style.display = 'none';
  }

  // ── Upload helper ─────────────────────────────────────────────────────────
  async function uploadFile(endpoint, file, cardId, statusId, type) {
    const card   = document.getElementById(cardId);
    const status = document.getElementById(statusId);

    card.classList.remove('uploaded', 'error');
    card.classList.add('uploading');
    status.textContent = 'Uploading…';
    showLoading(`Uploading ${file.name}…`);

    const fd = new FormData();
    fd.append('file', file);

    try {
      const res  = await fetch(endpoint, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Upload failed');

      card.classList.remove('uploading');
      card.classList.add('uploaded');

      let detail = `✓ ${file.name}`;
      if (type === 'csv' && data.data) {
        const d = data.data;
        detail += ` · ${d.columns.length} columns · ${(d.rows || 0).toLocaleString()} rows`;
        if (d.levels && d.levels.length) detail += ` · Level(s): ${d.levels.join(', ')}`;
      } else if (type === 'excel' && data.data) {
        detail += ` · ${data.data.sheets.length} sheet(s)`;
        if (data.data.fields_found) detail += ` · ${data.data.fields_found} fields parsed`;
      } else if (type === 'pdf' && data.data) {
        const bks = data.data.blocks_found || [];
        detail += bks.length ? ` · Blocks: ${bks.join(', ')}` : '';
        if (data.data.codebook_entries) detail += ` · ${data.data.codebook_entries} codebook entries`;
      }
      status.textContent = detail;
      uploaded[type] = true;
      updateButtons();
      return data;
    } catch (err) {
      card.classList.remove('uploading');
      card.classList.add('error');
      status.textContent = '✗ ' + err.message;
      uploaded[type] = false;
      updateButtons();
    } finally {
      hideLoading();
    }
  }

  // ── File input listeners ──────────────────────────────────────────────────
  inputCsv.addEventListener('change', e => {
    if (e.target.files[0]) uploadFile('/upload/csv', e.target.files[0], 'card-csv', 'status-csv', 'csv');
  });
  inputExcel.addEventListener('change', e => {
    if (e.target.files[0]) uploadFile('/upload/excel', e.target.files[0], 'card-excel', 'status-excel', 'excel');
  });
  inputPdf.addEventListener('change', e => {
    if (e.target.files[0]) uploadFile('/upload/pdf', e.target.files[0], 'card-pdf', 'status-pdf', 'pdf');
  });

  // ── Preview metadata ──────────────────────────────────────────────────────
  processBtn.addEventListener('click', async () => {
    processErr.style.display = 'none';
    processBtn.classList.add('running');
    processBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/></svg> Building…`;
    showLoading('Extracting levels, mapping columns, parsing PDF codes…');

    try {
      const res  = await fetch('/process', { method: 'POST' });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Processing failed');

      const s = data.stats;
      document.getElementById('stat-total').textContent   = s.total_columns;
      document.getElementById('stat-mapped').textContent  = s.mapped_columns;
      document.getElementById('stat-blocks').textContent  = s.blocks_identified;
      document.getElementById('stat-codes').textContent   = s.columns_with_codes;
      document.getElementById('stat-levels').textContent  = (s.levels || []).join(', ') || '—';

      statsSection.style.display = 'block';
      allResults = data.repository || [];
      populateBlockFilter(allResults);
      applyFilters();
      resultsSection.style.display = 'block';
      resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      processErr.textContent = err.message;
      processErr.style.display = 'block';
    } finally {
      processBtn.classList.remove('running');
      processBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/><polyline points="12 6 12 12 16 14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg> Preview Metadata Table`;
      hideLoading();
    }
  });

  // ── Generate full pipeline ────────────────────────────────────────────────
  generateBtn.addEventListener('click', async () => {
    generateErr.style.display = 'none';
    generateBtn.classList.add('running');
    generateBtn.innerHTML = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Running pipeline…`;
    showLoading('Running full consolidation pipeline — this may take a minute for large datasets…');

    try {
      const res  = await fetch('/generate', { method: 'POST' });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Generation failed');

      renderGenerateStats(data.stats);
      showOutputCards(data.outputs);
      outputsSection.style.display = 'block';
      outputsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (window._preprocSetReady) window._preprocSetReady(true);
    } catch (err) {
      generateErr.textContent = err.message;
      generateErr.style.display = 'block';
    } finally {
      generateBtn.classList.remove('running');
      generateBtn.innerHTML = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Run Full Pipeline &amp; Generate Outputs`;
      hideLoading();
    }
  });

  function renderGenerateStats(stats) {
    const grid = document.getElementById('gen-stats-grid');
    const items = [
      { label: 'Dataset Rows',        value: (stats.rows || 0).toLocaleString() },
      { label: 'Variables Resolved',  value: stats.variables_mapped || 0 },
      { label: 'Values Decoded',      value: stats.variables_coded || 0 },
      { label: 'Blocks Identified',   value: stats.blocks || 0 },
      { label: 'Codebook Entries',    value: stats.codebook_entries || 0 },
      { label: 'Survey Level(s)',      value: (stats.levels || []).join(', ') || '—' },
    ];
    grid.innerHTML = items.map(it =>
      `<div class="gen-stat-card">
        <div class="gen-stat-value">${it.value}</div>
        <div class="gen-stat-label">${it.label}</div>
      </div>`
    ).join('');
  }

  function showOutputCards(outputs) {
    // Handle parquet card availability
    const parquetCard = document.getElementById('out-parquet');
    if (!outputs.consolidated_parquet) {
      parquetCard.style.opacity = '0.45';
      parquetCard.style.pointerEvents = 'none';
      parquetCard.querySelector('.output-card-desc').textContent = 'Not available (pyarrow not installed)';
    } else {
      parquetCard.style.opacity = '';
      parquetCard.style.pointerEvents = '';
      parquetCard.classList.add('ready');
    }
    // Mark only the available (non-dimmed) cards as ready
    document.querySelectorAll('.output-card').forEach(card => {
      if (card !== parquetCard) card.classList.add('ready');
    });
  }

  // ── Reset ─────────────────────────────────────────────────────────────────
  resetBtn.addEventListener('click', async () => {
    if (!confirm('Reset all uploads and delete generated outputs?')) return;
    showLoading('Resetting…');
    try {
      await fetch('/reset', { method: 'POST' });
      uploaded.csv = uploaded.excel = uploaded.pdf = false;
      ['card-csv', 'card-excel', 'card-pdf'].forEach(id => {
        const c = document.getElementById(id);
        c.classList.remove('uploaded', 'uploading', 'error');
      });
      document.getElementById('status-csv').textContent   = 'Not uploaded';
      document.getElementById('status-excel').textContent = 'Not uploaded';
      document.getElementById('status-pdf').textContent   = 'Not uploaded';
      statsSection.style.display   = 'none';
      resultsSection.style.display = 'none';
      outputsSection.style.display = 'none';
      allResults      = [];
      filteredResults = [];
      tableBody.innerHTML = '';
      updateButtons();
    } finally {
      hideLoading();
    }
  });

  // ── Filters ───────────────────────────────────────────────────────────────
  function populateBlockFilter(data) {
    const blocks = [...new Set(data.map(r => String(r.block || '')).filter(Boolean))]
      .sort((a, b) => {
        const na = parseInt(a), nb = parseInt(b);
        return isNaN(na) || isNaN(nb) ? a.localeCompare(b) : na - nb;
      });
    filterBlock.innerHTML = '<option value="">All Blocks</option>';
    blocks.forEach(b => {
      const opt = document.createElement('option');
      opt.value = b;
      opt.textContent = `Block ${b}`;
      filterBlock.appendChild(opt);
    });
  }

  function applyFilters() {
    const q    = searchInput.value.toLowerCase().trim();
    const blk  = filterBlock.value.trim();
    const code = filterCodes.value;

    filteredResults = allResults.filter(r => {
      if (q && !(
        r.column_name.toLowerCase().includes(q) ||
        (r.full_name  || '').toLowerCase().includes(q) ||
        String(r.block || '').toLowerCase().includes(q) ||
        (r.question_text || '').toLowerCase().includes(q)
      )) return false;
      if (blk  && String(r.block || '').trim() !== blk)    return false;
      if (code === '1' && !(r.value_labels && r.value_labels.length)) return false;
      return true;
    });

    currentPage = 1;
    renderTable();
  }

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFilters, 220);
  });
  filterBlock.addEventListener('change', applyFilters);
  filterCodes.addEventListener('change', applyFilters);

  // ── Table render ──────────────────────────────────────────────────────────
  function renderTable() {
    tableBody.innerHTML = '';

    if (!filteredResults.length) {
      noResults.style.display = 'block';
      document.getElementById('meta-table').style.display = 'none';
      pagination.innerHTML = '';
      return;
    }
    noResults.style.display = 'none';
    document.getElementById('meta-table').style.display = 'table';

    const start = (currentPage - 1) * PAGE_SIZE;
    const page  = filteredResults.slice(start, start + PAGE_SIZE);

    page.forEach(r => {
      const tr = document.createElement('tr');

      const valuesHtml = (r.value_labels && r.value_labels.length)
        ? `<ul class="values-list">${r.value_labels.map(v =>
            `<li><span class="val-code">${escHtml(v.code)}</span><span class="val-label">${escHtml(v.label)}</span></li>`
          ).join('')}</ul>`
        : `<span class="no-value">—</span>`;

      const blockHtml = r.block
        ? `<span class="block-badge">Block ${escHtml(String(r.block))}</span>`
        : `<span class="no-value">—</span>`;

      const levelHtml = r.level
        ? `<span class="level-badge">${escHtml(String(r.level))}</span>`
        : `<span class="no-value">—</span>`;

      const itemCol = [r.item, r.col].filter(Boolean).join(' / ');

      tr.innerHTML = `
        <td><span class="col-name">${escHtml(r.column_name)}</span></td>
        <td>${escHtml(r.full_name || '—')}</td>
        <td>${blockHtml}</td>
        <td>${escHtml(itemCol || '—')}</td>
        <td>${levelHtml}</td>
        <td>${escHtml(r.question_text || '—')}</td>
        <td>${valuesHtml}</td>
      `;
      tableBody.appendChild(tr);
    });

    renderPagination();
  }

  function renderPagination() {
    const total = Math.ceil(filteredResults.length / PAGE_SIZE);
    pagination.innerHTML = '';
    if (total <= 1) return;

    const makeBtn = (label, page, active, disabled) => {
      const btn = document.createElement('button');
      btn.className = 'page-btn' + (active ? ' active' : '');
      btn.textContent = label;
      btn.disabled = disabled;
      if (!disabled) btn.addEventListener('click', () => { currentPage = page; renderTable(); });
      return btn;
    };

    pagination.appendChild(makeBtn('‹ Prev', currentPage - 1, false, currentPage === 1));
    let start = Math.max(1, currentPage - 2);
    let end   = Math.min(total, start + 4);
    if (end - start < 4) start = Math.max(1, end - 4);
    for (let p = start; p <= end; p++) {
      pagination.appendChild(makeBtn(String(p), p, p === currentPage, false));
    }
    pagination.appendChild(makeBtn('Next ›', currentPage + 1, false, currentPage === total));
  }

  // ── Export metadata CSV ───────────────────────────────────────────────────
  exportBtn.addEventListener('click', () => {
    if (!filteredResults.length) return;
    const headers = ['column_name', 'full_name', 'block', 'item', 'col', 'level', 'question_text', 'value_labels'];
    const rows = filteredResults.map(r => [
      r.column_name, r.full_name, r.block, r.item, r.col, r.level,
      r.question_text,
      (r.value_labels || []).map(v => `${v.code}=${v.label}`).join('; '),
    ]);
    const csv = [headers, ...rows].map(row =>
      row.map(v => `"${String(v || '').replace(/"/g, '""')}"`).join(',')
    ).join('\n');
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(new Blob([csv], { type: 'text/csv' })),
      download: 'datavizai_metadata_repository.csv',
    });
    a.click();
  });

  // ── Utility ───────────────────────────────────────────────────────────────
  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Init: restore server state ────────────────────────────────────────────
  (async function init() {
    try {
      const res  = await fetch('/status');
      const data = await res.json();

      if (data.csv_uploaded) {
        uploaded.csv = true;
        document.getElementById('card-csv').classList.add('uploaded');
        document.getElementById('status-csv').textContent = `✓ ${data.csv_filename}`;
      }
      if (data.excel_uploaded) {
        uploaded.excel = true;
        document.getElementById('card-excel').classList.add('uploaded');
        document.getElementById('status-excel').textContent = `✓ ${data.excel_filename}`;
      }
      if (data.pdf_uploaded) {
        uploaded.pdf = true;
        document.getElementById('card-pdf').classList.add('uploaded');
        document.getElementById('status-pdf').textContent = `✓ ${data.pdf_filename}`;
      }

      updateButtons();

      // Restore generate outputs if already done
      if (data.generated && data.generate_stats && data.generate_outputs) {
        renderGenerateStats(data.generate_stats);
        showOutputCards(data.generate_outputs);
        outputsSection.style.display = 'block';
        if (window._preprocSetReady) window._preprocSetReady(true);
      }
    } catch (_) {}
  })();
})();

// ═══════════════════════════════════════════════════════════════════════════
// STEP 4 — Data Preprocessing (Detect → Review → Apply)
// ═══════════════════════════════════════════════════════════════════════════
(function () {
  'use strict';

  const detectBtn      = document.getElementById('preproc-detect-btn');
  const detectError    = document.getElementById('preproc-detect-error');
  const reviewPanel    = document.getElementById('preproc-review-panel');
  const reviewSummary  = document.getElementById('preproc-review-summary');
  const reviewVars     = document.getElementById('preproc-review-vars');
  const phaseRun       = document.getElementById('preproc-phase-run');
  const preprocRunBtn  = document.getElementById('preproc-run-btn');
  const preprocError   = document.getElementById('preproc-error');
  const preprocResults = document.getElementById('preproc-results');
  const loadingOverlay = document.getElementById('loading-overlay');
  const loadingText    = document.getElementById('loading-text');

  function showLoading(t) { loadingText.textContent = t || 'Processing…'; loadingOverlay.style.display = 'flex'; }
  function hideLoading()  { loadingOverlay.style.display = 'none'; }
  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function getVal(name) {
    const r = document.querySelector(`input[name="${name}"]:checked`);
    return r ? r.value : null;
  }

  // ── Persistent state (survives dropdown open/close within session) ────────
  // skippedRows: Set of row __row_index__ values excluded from ALL preprocessing
  // editedCells: { rowIndex: { col: newValue } }
  const skippedRows = new Set();
  const editedCells = {};  // { "rowIndex": { col: val } }

  // ── Method description notes ───────────────────────────────────────────
  const missingNotes = {
    median: 'Replaces missing values in numeric columns with the column median. Robust to skewed data.',
    mean:   'Replaces missing values with the column mean. Best for normally distributed data.',
    mode:   'Replaces missing values with the most frequent value. Suitable for categorical-like integers.',
    knn:    'K-Nearest Neighbours imputation (k=5). Considers similar rows to estimate missing values.',
    none:   'Missing value imputation will be skipped.',
  };
  const outlierNotes = {
    iqr:             'Flags values outside Q1 − 1.5×IQR and Q3 + 1.5×IQR. Robust and widely used.',
    z_score:         'Flags values with |z| > 3 (more than 3 standard deviations from the mean).',
    modified_z_score:'Flags values using the median absolute deviation (|mz| > 3.5). More robust than Z-Score for skewed data.',
    percentile:      'Flags values below the 1st or above the 99th percentile.',
    none:            'Outlier detection will be skipped.',
  };

  function updateNotes() {
    const mn = document.getElementById('preproc-missing-note');
    const on = document.getElementById('preproc-outlier-note');
    const ag = document.getElementById('preproc-action-group');
    const mv = getVal('preproc-missing');
    const ov = getVal('preproc-outlier');
    if (mn && mv) mn.textContent = missingNotes[mv] || '';
    if (on && ov) on.textContent = outlierNotes[ov] || '';
    if (ag) ag.style.opacity = ov === 'none' ? '0.45' : '1';
  }
  document.querySelectorAll('input[name="preproc-missing"], input[name="preproc-outlier"]')
    .forEach(r => r.addEventListener('change', updateNotes));
  updateNotes();

  // ── Enable detect button when Step 3 is done ──────────────────────────
  window._preprocSetReady = function (ready) {
    if (detectBtn) detectBtn.disabled = !ready;
  };

  // ── PHASE 1: Detect ───────────────────────────────────────────────────
  detectBtn.addEventListener('click', async () => {
    detectError.style.display = 'none';
    detectBtn.disabled = true;
    detectBtn.textContent = 'Detecting…';
    showLoading('Scanning for missing values and outliers…');

    try {
      const res  = await fetch('/preprocess/detect', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ outlier_method: getVal('preproc-outlier') }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Detection failed');

      // Clear previous state when re-detecting
      skippedRows.clear();
      Object.keys(editedCells).forEach(k => delete editedCells[k]);

      renderReviewPanel(data);
      reviewPanel.style.display = 'block';
      phaseRun.style.display    = 'block';
      reviewPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      detectError.textContent = err.message;
      detectError.style.display = 'block';
    } finally {
      detectBtn.disabled = false;
      detectBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="8" stroke="currentColor" stroke-width="2"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg> Detect Missing Values &amp; Outliers`;
      hideLoading();
    }
  });

  // ── Render the review panel ────────────────────────────────────────────
  function renderReviewPanel(data) {
    const mv = data.missing_vars || [];
    const ov = data.outlier_vars || [];
    const totalMissingRows = mv.reduce((s, v) => s + v.count, 0);
    const totalOutlierRows = ov.reduce((s, v) => s + v.count, 0);

    reviewSummary.innerHTML = `
      <div class="preproc-detect-summary">
        <div class="preproc-detect-badge preproc-badge-missing">
          <strong>${mv.length}</strong> variable${mv.length !== 1 ? 's' : ''} with missing values
          <span class="preproc-badge-sub">(${totalMissingRows.toLocaleString()} affected rows)</span>
        </div>
        <div class="preproc-detect-badge preproc-badge-outlier">
          <strong>${ov.length}</strong> variable${ov.length !== 1 ? 's' : ''} with outliers
          <span class="preproc-badge-sub">(${totalOutlierRows.toLocaleString()} affected rows)</span>
        </div>
        <div class="preproc-detect-hint">
          Click a variable to expand its flagged rows. Uncheck a row to skip it from preprocessing.
          Edit any cell value directly — edited cells are treated as final and excluded from imputation.
        </div>
      </div>`;

    let html = '';

    if (mv.length === 0 && ov.length === 0) {
      html = '<div class="preproc-no-issues">No missing values or outliers detected in numeric columns.</div>';
    }

    if (mv.length > 0) {
      html += `<div class="preproc-var-group-title">Missing Values</div>`;
      mv.forEach(v => { html += buildVarAccordion(v, 'missing'); });
    }
    if (ov.length > 0) {
      html += `<div class="preproc-var-group-title">Outliers</div>`;
      ov.forEach(v => { html += buildVarAccordion(v, 'outlier'); });
    }

    reviewVars.innerHTML = html;

    // Attach toggle listeners
    reviewVars.querySelectorAll('.preproc-var-header').forEach(hdr => {
      hdr.addEventListener('click', () => {
        const acc = hdr.closest('.preproc-var-accordion');
        acc.classList.toggle('open');
      });
    });

    attachRowListeners(reviewVars);
    attachPaginationListeners(reviewVars);
  }

  // ── Attach checkbox + editable-cell listeners to a container ──────────
  function attachRowListeners(container) {
    container.querySelectorAll('.preproc-row-check').forEach(cb => {
      const rowIdx = parseInt(cb.dataset.rowIdx, 10);
      cb.checked = !skippedRows.has(rowIdx);
      cb.addEventListener('change', () => {
        if (cb.checked) skippedRows.delete(rowIdx);
        else             skippedRows.add(rowIdx);
        reviewVars.querySelectorAll(`.preproc-row-check[data-row-idx="${rowIdx}"]`).forEach(other => {
          other.checked = cb.checked;
        });
      });
    });

    container.querySelectorAll('.preproc-cell-edit').forEach(cell => {
      const rowIdx = cell.dataset.rowIdx;
      const col    = cell.dataset.col;
      if (editedCells[rowIdx] && editedCells[rowIdx][col] !== undefined) {
        cell.textContent = editedCells[rowIdx][col];
        cell.classList.add('edited');
      }
      cell.addEventListener('blur', () => {
        const val = cell.textContent.trim();
        if (!editedCells[rowIdx]) editedCells[rowIdx] = {};
        editedCells[rowIdx][col] = val;
        cell.classList.add('edited');
      });
      cell.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); cell.blur(); }
      });
    });
  }

  // ── Paginated row-loading for large detection results ─────────────────
  // Only attach to top pagination bar (not the mirrored bottom one) to avoid double-firing.
  function attachPaginationListeners(container) {
    container.querySelectorAll('.detect-pagination:not(.detect-pagination-bottom)').forEach(pag => {
      const col  = pag.dataset.col;
      const kind = pag.dataset.kind;

      async function loadPage(page) {
        const accordion = pag.closest('.preproc-var-accordion');
        const tbody = accordion.querySelector('.preproc-row-table tbody');
        tbody.innerHTML = `<tr><td colspan="99" class="detect-page-loading">Loading page ${page}…</td></tr>`;

        try {
          const res = await fetch('/preprocess/detect/rows', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ kind, col, page }),
          });
          const data = await res.json();
          if (!res.ok || data.error) throw new Error(data.error || 'Failed to load rows');

          const allCols = data.all_columns || [];
          tbody.innerHTML = data.rows.map(row => {
            const rowIdx = row['__row_index__'];
            const tdCells = allCols.map(c => {
              const val      = row[c];
              const dispVal  = val === null || val === undefined ? '' : String(val);
              if (c === col) {
                return `<td class="preproc-td preproc-td-flagged">
                  <span class="preproc-cell-edit" contenteditable="true"
                    data-row-idx="${rowIdx}" data-col="${esc(c)}">${esc(dispVal)}</span>
                </td>`;
              }
              return `<td class="preproc-td">${esc(dispVal)}</td>`;
            }).join('');
            return `<tr class="preproc-row">
              <td class="preproc-td preproc-td-check">
                <input type="checkbox" class="preproc-row-check" data-row-idx="${rowIdx}"
                  ${skippedRows.has(rowIdx) ? '' : 'checked'} title="Include this row in preprocessing">
              </td>
              ${tdCells}
            </tr>`;
          }).join('');

          attachRowListeners(tbody);

          // Sync both top and bottom pagination bars in this accordion
          accordion.querySelectorAll('.detect-pagination').forEach(p => {
            p.dataset.currentPage = page;
            const totalPages = parseInt(p.dataset.totalPages);
            p.querySelector('.detect-page-info').textContent =
              `Page ${page} of ${totalPages} (${parseInt(p.dataset.totalCount).toLocaleString()} rows total)`;
            p.querySelector('.detect-prev-btn').disabled = page <= 1;
            p.querySelector('.detect-next-btn').disabled = page >= totalPages;
          });
        } catch (err) {
          tbody.innerHTML = `<tr><td colspan="99" style="color:red;padding:1rem;">Error: ${esc(err.message)}</td></tr>`;
        }
      }

      // Wire up BOTH top and bottom prev/next buttons to the same loadPage handler
      accordion_scope: {
        const accordion = pag.closest('.preproc-var-accordion');
        accordion.querySelectorAll('.detect-pagination').forEach(anyPag => {
          anyPag.querySelector('.detect-prev-btn').addEventListener('click', () => {
            const cur = parseInt(pag.dataset.currentPage);
            if (cur > 1) loadPage(cur - 1);
          });
          anyPag.querySelector('.detect-next-btn').addEventListener('click', () => {
            const cur   = parseInt(pag.dataset.currentPage);
            const total = parseInt(pag.dataset.totalPages);
            if (cur < total) loadPage(cur + 1);
          });
        });
      }
    });
  }

  const DETECT_PAGE_SIZE = 500;

  function buildVarAccordion(v, kind) {
    const col      = v.column;
    const count    = v.count;
    const allCols  = v.all_columns || [];
    const rows     = v.rows || [];
    const isMiss   = kind === 'missing';
    const totalPages = Math.ceil(count / DETECT_PAGE_SIZE);

    const badge = isMiss
      ? `<span class="preproc-var-badge preproc-badge-missing-sm">${count.toLocaleString()} missing</span>`
      : `<span class="preproc-var-badge preproc-badge-outlier-sm">${count.toLocaleString()} outliers
           ${v.lo !== null ? `<span style="font-weight:400;opacity:.8"> [${v.lo}, ${v.hi}]</span>` : ''}
         </span>`;

    // Build table header — all columns, but highlight flagged one
    const thCells = ['', ...allCols].map(c => {
      if (c === '') return '<th class="preproc-th preproc-th-check">Include</th>';
      const cls = c === col ? 'preproc-th preproc-th-flagged' : 'preproc-th';
      return `<th class="${cls}">${esc(c)}</th>`;
    }).join('');

    // Build table rows (first page, already in response)
    const trRows = rows.map(row => {
      const rowIdx = row['__row_index__'];
      const tdCells = allCols.map(c => {
        const val = row[c];
        const dispVal = val === null || val === undefined ? '' : String(val);
        if (c === col) {
          return `<td class="preproc-td preproc-td-flagged">
            <span class="preproc-cell-edit" contenteditable="true"
              data-row-idx="${rowIdx}" data-col="${esc(c)}">${esc(dispVal)}</span>
          </td>`;
        }
        return `<td class="preproc-td">${esc(dispVal)}</td>`;
      }).join('');
      return `<tr class="preproc-row">
        <td class="preproc-td preproc-td-check">
          <input type="checkbox" class="preproc-row-check" data-row-idx="${rowIdx}" checked title="Include this row in preprocessing">
        </td>
        ${tdCells}
      </tr>`;
    }).join('');

    // Pagination controls (only shown when total > 500)
    const paginationHtml = totalPages > 1 ? `
      <div class="detect-pagination"
           data-col="${esc(col)}" data-kind="${kind}"
           data-current-page="1"
           data-total-pages="${totalPages}"
           data-total-count="${count}">
        <button class="detect-page-btn detect-prev-btn" disabled>‹ Prev</button>
        <span class="detect-page-info">Page 1 of ${totalPages} (${count.toLocaleString()} rows total)</span>
        <button class="detect-page-btn detect-next-btn">Next ›</button>
      </div>` : '';

    return `
      <div class="preproc-var-accordion" data-col="${esc(col)}" data-kind="${kind}">
        <div class="preproc-var-header">
          <span class="preproc-var-chevron">▶</span>
          <span class="preproc-var-colname">${esc(col)}</span>
          ${badge}
        </div>
        <div class="preproc-var-body">
          ${paginationHtml}
          <div class="preproc-table-wrap">
            <table class="preproc-row-table">
              <thead><tr>${thCells}</tr></thead>
              <tbody>${trRows}</tbody>
            </table>
          </div>
          ${paginationHtml ? paginationHtml.replace('class="detect-pagination"', 'class="detect-pagination detect-pagination-bottom"') : ''}
        </div>
      </div>`;
  }

  // ── PHASE 2: Apply preprocessing ──────────────────────────────────────
  preprocRunBtn.addEventListener('click', async () => {
    preprocError.style.display   = 'none';
    preprocResults.style.display = 'none';
    preprocRunBtn.disabled = true;
    preprocRunBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 17l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Applying…`;
    showLoading('Applying preprocessing…');

    try {
      const res  = await fetch('/preprocess/run', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          missing_method: getVal('preproc-missing'),
          outlier_method: getVal('preproc-outlier'),
          outlier_action: getVal('preproc-action'),
          skipped_rows:   Array.from(skippedRows),
          edited_cells:   editedCells,
        }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Preprocessing failed');

      renderPreprocResults(data.report);
      preprocResults.style.display = 'block';
      preprocResults.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      preprocError.textContent   = err.message;
      preprocError.style.display = 'block';
    } finally {
      preprocRunBtn.disabled = false;
      preprocRunBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 17l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Apply Preprocessing`;
      hideLoading();
    }
  });

  function renderPreprocResults(r) {
    const rowsRemoved  = r.rows_removed  || 0;
    const missingCols  = r.columns_with_missing  || [];
    const outlierCols  = r.columns_with_outliers || [];

    const missingColsHtml = missingCols.length
      ? `<div class="preproc-col-list">
          ${missingCols.map(c =>
            `<div class="preproc-col-item">
              <span class="preproc-col-name">${esc(c.column)}</span>
              <span class="preproc-col-stat">${c.imputed.toLocaleString()} imputed → fill=${c.fill_value}</span>
            </div>`).join('')}
         </div>`
      : '<div class="preproc-col-none">No missing values found in numeric columns.</div>';

    const outlierColsHtml = outlierCols.length
      ? `<div class="preproc-col-list">
          ${outlierCols.map(c =>
            `<div class="preproc-col-item">
              <span class="preproc-col-name">${esc(c.column)}</span>
              <span class="preproc-col-stat">${c.count.toLocaleString()} (${c.pct}%)</span>
              ${c.lo !== null ? `<span class="preproc-col-bounds">[${c.lo}, ${c.hi}]</span>` : ''}
            </div>`).join('')}
         </div>`
      : '<div class="preproc-col-none">No outliers detected in continuous columns.</div>';

    preprocResults.innerHTML = `
      <div class="preproc-success-banner">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="22 4 12 14.01 9 11.01" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Preprocessing complete — preprocessed_dataset.csv saved and ready for Step 5.
        <a href="/download/preprocessed" download="preprocessed_dataset.csv" class="preproc-download-btn">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="7 10 12 15 17 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><line x1="12" y1="15" x2="12" y2="3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          Download Preprocessed Dataset
        </a>
      </div>
      <div class="preproc-stat-grid">
        <div class="preproc-stat-card">
          <div class="preproc-stat-value">${(r.rows_before || 0).toLocaleString()}</div>
          <div class="preproc-stat-label">Rows Before</div>
        </div>
        <div class="preproc-stat-card">
          <div class="preproc-stat-value">${(r.rows_after || 0).toLocaleString()}</div>
          <div class="preproc-stat-label">Rows After</div>
        </div>
        <div class="preproc-stat-card ${r.missing_imputed > 0 ? 'preproc-stat-highlight' : ''}">
          <div class="preproc-stat-value">${(r.missing_imputed || 0).toLocaleString()}</div>
          <div class="preproc-stat-label">Missing Imputed</div>
        </div>
        <div class="preproc-stat-card ${r.outliers_detected > 0 ? 'preproc-stat-highlight' : ''}">
          <div class="preproc-stat-value">${(r.outliers_detected || 0).toLocaleString()}</div>
          <div class="preproc-stat-label">Outliers Detected</div>
        </div>
        <div class="preproc-stat-card">
          <div class="preproc-stat-value">${(r.outliers_handled || 0).toLocaleString()}</div>
          <div class="preproc-stat-label">Outliers Handled</div>
        </div>
        ${r.rows_skipped > 0 ? `<div class="preproc-stat-card preproc-stat-warn">
          <div class="preproc-stat-value">${r.rows_skipped.toLocaleString()}</div>
          <div class="preproc-stat-label">Rows Skipped</div>
        </div>` : ''}
        ${r.cells_edited > 0 ? `<div class="preproc-stat-card preproc-stat-highlight">
          <div class="preproc-stat-value">${r.cells_edited.toLocaleString()}</div>
          <div class="preproc-stat-label">Cells Edited</div>
        </div>` : ''}
        ${rowsRemoved > 0 ? `<div class="preproc-stat-card preproc-stat-warn">
          <div class="preproc-stat-value">${rowsRemoved.toLocaleString()}</div>
          <div class="preproc-stat-label">Rows Removed</div>
        </div>` : ''}
      </div>
      <div class="preproc-detail-cols">
        <div class="preproc-detail-section">
          <div class="preproc-detail-title">Columns with Missing Values (${missingCols.length})</div>
          ${missingColsHtml}
        </div>
        <div class="preproc-detail-section">
          <div class="preproc-detail-title">Columns with Outliers (${outlierCols.length})</div>
          ${outlierColsHtml}
        </div>
      </div>`;
  }
})();

// ═══════════════════════════════════════════════════════════════════════════
// SURVEY TABLE BUILDER v2.0 — Metadata-Driven DataVizAI Engine
// ═══════════════════════════════════════════════════════════════════════════
(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────────
  let tbAllColumns   = [];
  let tbFilteredCols = [];
  let tbSelectedCols = new Set();
  let tbVarRoles     = {};   // col -> 'dimension'|'measure'|'weight'|'unassigned'
  let tbVarMeta      = {};   // col -> full metadata object
  let tbWeightCol    = null;
  let tbTableCounter = 0;
  let tbTableDefs    = [];   // [{id, el}]
  let tbProcessedVars = [];

  // ── DOM refs ────────────────────────────────────────────────────────────
  const tbLoadBtn        = document.getElementById('tb-load-btn');
  const tbLoadError      = document.getElementById('tb-load-error');
  const tbVarPanel       = document.getElementById('tb-var-panel');
  const tbVarGrid        = document.getElementById('tb-var-grid');
  const tbVarSearch      = document.getElementById('tb-var-search');
  const tbVarCatFilter   = document.getElementById('tb-var-cat-filter');
  const tbRoleFilter     = document.getElementById('tb-role-filter');
  const tbSelAll         = document.getElementById('tb-sel-all');
  const tbDeselAll       = document.getElementById('tb-desel-all');
  const tbSelCount       = document.getElementById('tb-sel-count');
  const tbTotalCount     = document.getElementById('tb-total-count');
  const tbWeightDisplay  = document.getElementById('tb-weight-display');
  const tbWeightOverride = document.getElementById('tb-weight-override');
  const tbPreprocBtn     = document.getElementById('tb-preprocess-btn');
  const tbDiagBtn        = document.getElementById('tb-diag-btn');
  const tbPreprocError   = document.getElementById('tb-preprocess-error');
  const tbPreprocStats   = document.getElementById('tb-preprocess-stats');
  const tbDiagPanel      = document.getElementById('tb-diag-panel');
  const tbConfigPanel    = document.getElementById('tb-config-panel');
  const tbTablesContainer = document.getElementById('tb-tables-container');
  const tbAddTableBtn    = document.getElementById('tb-add-table-btn');
  const tbGenerateBtn    = document.getElementById('tb-generate-btn');
  const tbGenerateError  = document.getElementById('tb-generate-error');
  const tbResultsPanel   = document.getElementById('tb-results-panel');
  const tbResultsCont    = document.getElementById('tb-results-container');
  const tbGenWeightCol   = document.getElementById('tb-gen-weight-col');
  const tbGenWeightLabel = document.getElementById('tb-gen-weight-label');

  const loadingOverlay = document.getElementById('loading-overlay');
  const loadingText    = document.getElementById('loading-text');
  function showLoading(t) { loadingText.textContent = t || 'Processing…'; loadingOverlay.style.display = 'flex'; }
  function hideLoading() { loadingOverlay.style.display = 'none'; }
  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function getDataSrc() { const r = document.querySelector('input[name="tb-data-src"]:checked'); return r ? r.value : 'processed'; }

  // ── Load Variables ───────────────────────────────────────────────────────
  tbLoadBtn.addEventListener('click', async () => {
    tbLoadError.style.display = 'none';
    tbLoadBtn.disabled = true;
    // Reset Phase 2 & 3 so user can re-select columns and regenerate freely
    tbSelectedCols  = new Set();
    tbVarRoles      = {};
    tbVarMeta       = {};
    tbProcessedVars = [];
    tbTableDefs     = [];
    tbTableCounter  = 0;
    tbConfigPanel.style.display   = 'none';
    tbResultsPanel.style.display  = 'none';
    tbPreprocStats.style.display  = 'none';
    tbDiagPanel.style.display     = 'none';
    tbDiagBtn.style.display       = 'none';
    tbTablesContainer.innerHTML   = '';
    tbResultsCont.innerHTML       = '';
    showLoading('Loading column metadata from consolidated dataset…');
    try {
      const res  = await fetch('/table-builder/columns');
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Failed to load columns.');

      tbAllColumns = data.columns || [];
      tbWeightCol  = data.weight_column || null;
      tbFilteredCols = tbAllColumns;

      // Initialise role and meta maps
      tbVarRoles = {};
      tbVarMeta  = {};
      tbAllColumns.forEach(c => {
        tbVarRoles[c.column] = c.auto_role || 'unassigned';
        tbVarMeta[c.column]  = c;
      });

      tbTotalCount.textContent = tbAllColumns.length;
      tbWeightDisplay.textContent = tbWeightCol || 'None detected';

      // Populate weight-override dropdown
      tbWeightOverride.innerHTML = '<option value="">— Override weight column —</option>';
      tbAllColumns.forEach(c => {
        tbWeightOverride.appendChild(new Option(c.column, c.column));
      });
      if (tbWeightCol) tbWeightOverride.value = tbWeightCol;

      renderVarGrid();
      tbVarPanel.style.display = 'block';
      tbVarPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      tbLoadError.textContent = err.message;
      tbLoadError.style.display = 'block';
    } finally {
      tbLoadBtn.disabled = false;
      tbLoadBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="7 10 12 15 17 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><line x1="12" y1="15" x2="12" y2="3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg> Load Variables from Dataset`;
      hideLoading();
    }
  });

  // Weight override
  tbWeightOverride.addEventListener('change', () => {
    const prev = tbWeightCol;
    tbWeightCol = tbWeightOverride.value || tbWeightCol;
    // Re-classify roles: demote old weight, promote new
    if (prev && tbVarRoles[prev] === 'weight') tbVarRoles[prev] = tbVarMeta[prev] ? tbVarMeta[prev].auto_role : 'unassigned';
    if (tbWeightCol) tbVarRoles[tbWeightCol] = 'weight';
    tbWeightDisplay.textContent = tbWeightCol || 'None';
    renderVarGrid();
    syncWeightColDropdown();
  });

  // ── Render variable grid ─────────────────────────────────────────────────
  function renderVarGrid() {
    tbVarGrid.innerHTML = '';
    const q    = (tbVarSearch.value || '').toLowerCase().trim();
    const cat  = tbVarCatFilter.value;
    const role = tbRoleFilter.value;

    tbFilteredCols = tbAllColumns.filter(c => {
      if (q && !c.column.toLowerCase().includes(q)) return false;
      if (cat && c.category !== cat) return false;
      if (role && tbVarRoles[c.column] !== role) return false;
      return true;
    });

    tbFilteredCols.forEach(col => {
      const curRole   = tbVarRoles[col.column] || 'unassigned';
      const isChecked = tbSelectedCols.has(col.column);
      const catClass  = col.category === 'Continuous' ? 'cat-continuous'
                      : col.category === 'Categorical (Numeric)' ? 'cat-cat-num'
                      : 'cat-cat-text';

      const card = document.createElement('div');
      card.className = 'tb-var-card' + (isChecked ? ' selected' : '') + (curRole === 'weight' ? ' is-weight' : '');
      card.dataset.col = col.column;

      const sampleHtml = col.sample_values.slice(0, 5).map(v => `<span class="tb-sample-val">${esc(v)}</span>`).join('');

      card.innerHTML = `
        <div class="tb-var-card-top">
          <label class="tb-var-label">
            <input type="checkbox" class="tb-var-cb" ${isChecked ? 'checked' : ''} />
            <div class="tb-var-info">
              <div class="tb-var-name">${esc(col.column)}</div>
              <div class="tb-var-meta">
                <span class="tb-cat-badge ${catClass}">${esc(col.category)}</span>
                <span class="tb-meta-chip">${col.n_unique} unique</span>
                ${col.n_missing > 0 ? `<span class="tb-meta-chip tb-missing-chip">${col.pct_missing}% missing</span>` : ''}
              </div>
              <div class="tb-sample-vals">${sampleHtml}</div>
            </div>
          </label>
          <select class="tb-role-sel ${curRole === 'weight' ? 'disabled-sel' : ''}" ${curRole === 'weight' ? 'disabled' : ''}>
            <option value="dimension" ${curRole === 'dimension' ? 'selected' : ''}>Dimension</option>
            <option value="measure"   ${curRole === 'measure'   ? 'selected' : ''}>Measure</option>
            <option value="weight"    ${curRole === 'weight'    ? 'selected' : ''}>Weight</option>
            <option value="unassigned"${curRole === 'unassigned'? 'selected' : ''}>Unassigned</option>
          </select>
        </div>
        <div class="tb-role-bar">
          <span class="tb-role-badge role-${curRole}">${curRole.charAt(0).toUpperCase() + curRole.slice(1)}</span>
        </div>
      `;

      card.querySelector('.tb-var-cb').addEventListener('change', e => {
        if (e.target.checked) tbSelectedCols.add(col.column);
        else tbSelectedCols.delete(col.column);
        card.classList.toggle('selected', e.target.checked);
        updateSelCount();
      });

      const roleSel = card.querySelector('.tb-role-sel');
      roleSel.addEventListener('change', () => {
        const newRole = roleSel.value;
        // If assigning weight: demote old weight
        if (newRole === 'weight') {
          const prevWeight = tbWeightCol;
          if (prevWeight && prevWeight !== col.column && tbVarRoles[prevWeight] === 'weight') {
            tbVarRoles[prevWeight] = tbVarMeta[prevWeight] ? tbVarMeta[prevWeight].auto_role : 'unassigned';
          }
          tbWeightCol = col.column;
          tbWeightDisplay.textContent = col.column;
          tbWeightOverride.value = col.column;
          syncWeightColDropdown();
        }
        tbVarRoles[col.column] = newRole;
        renderVarGrid();
        refreshTableDefDropdowns();
      });

      tbVarGrid.appendChild(card);
    });

    updateSelCount();
  }

  function updateSelCount() { tbSelCount.textContent = tbSelectedCols.size; }

  tbVarSearch.addEventListener('input', () => renderVarGrid());
  tbVarCatFilter.addEventListener('change', () => renderVarGrid());
  tbRoleFilter.addEventListener('change', () => renderVarGrid());

  tbSelAll.addEventListener('click', () => {
    tbFilteredCols.forEach(c => tbSelectedCols.add(c.column));
    renderVarGrid();
  });
  tbDeselAll.addEventListener('click', () => {
    tbFilteredCols.forEach(c => tbSelectedCols.delete(c.column));
    renderVarGrid();
  });

  function syncWeightColDropdown() {
    const all = getSelectedVars();
    tbGenWeightCol.innerHTML = '';
    // Always include the auto-detected weight column first, even if the user
    // did not select it in Phase 1 (it is excluded from the variable grid
    // but must be available as a weight option for table generation).
    if (tbWeightCol && !all.includes(tbWeightCol)) {
      tbGenWeightCol.appendChild(new Option(tbWeightCol, tbWeightCol));
    }
    all.forEach(v => tbGenWeightCol.appendChild(new Option(v, v)));
    if (tbWeightCol) tbGenWeightCol.value = tbWeightCol;
  }

  // ── Preprocessing ────────────────────────────────────────────────────────
  tbPreprocBtn.addEventListener('click', async () => {
    tbPreprocError.style.display = 'none';
    if (tbSelectedCols.size === 0) {
      tbPreprocError.textContent = 'Please select at least one variable.';
      tbPreprocError.style.display = 'block';
      return;
    }
    const selected = Array.from(tbSelectedCols);
    tbPreprocBtn.disabled = true;
    showLoading('Applying preprocessing — cleaning, missing imputation, outlier capping…');
    try {
      const res  = await fetch('/table-builder/preprocess', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ selected_variables: selected }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Preprocessing failed.');

      const s = data.stats;
      tbProcessedVars = selected;

      tbPreprocStats.innerHTML = `
        <div class="tb-preproc-grid">
          <div class="tb-preproc-card"><div class="tb-preproc-val">${s.rows.toLocaleString()}</div><div class="tb-preproc-lbl">Rows Processed</div></div>
          <div class="tb-preproc-card"><div class="tb-preproc-val">${s.selected_columns}</div><div class="tb-preproc-lbl">Variables Selected</div></div>
          <div class="tb-preproc-card"><div class="tb-preproc-val">${s.missing_treated.toLocaleString()}</div><div class="tb-preproc-lbl">Missing Cells Treated</div></div>
          <div class="tb-preproc-card"><div class="tb-preproc-val">${esc(s.weight_column || '—')}</div><div class="tb-preproc-lbl">Weight Column</div></div>
        </div>
        <div class="tb-preproc-note">✓ Preprocessing steps: ${(s.preprocessing_steps || []).join(', ') || 'none'} · Saved as <code>processed_selected_dataset.csv</code></div>
      `;
      tbPreprocStats.style.display = 'block';
      tbDiagBtn.style.display = 'inline-flex';

      // Populate weight dropdown in Phase 2
      syncWeightColDropdown();
      tbConfigPanel.style.display = 'block';
      if (tbTableDefs.length === 0) {
        addTableDef();
      } else {
        // Existing table defs: remove stale items and refresh dropdowns
        const activeVars = new Set(getSelectedVars());
        tbTableDefs.forEach(({ el }) => {
          // Drop dimension items that are no longer selected
          el.querySelectorAll('.tb-dim-item').forEach(item => {
            if (!activeVars.has(item.dataset.var)) item.remove();
          });
          refreshDimLevels(el.querySelector('.tb-dims-list'));

          // Drop measure items that are no longer selected
          el.querySelectorAll('.tb-meas-item').forEach(item => {
            if (!activeVars.has(item.dataset.var)) item.remove();
          });

          // Re-populate add-dropdowns with new variable set
          populateTableDefDropdowns(el);

          // Auto-add newly assigned dim vars not already in the list
          const existingDims = new Set(
            Array.from(el.querySelectorAll('.tb-dim-item')).map(i => i.dataset.var)
          );
          getDimVars().forEach(v => { if (!existingDims.has(v)) addDimItem(el, v); });

          // Auto-add newly assigned measure vars not already in the list
          const existingMeas = new Set(
            Array.from(el.querySelectorAll('.tb-meas-item')).map(i => i.dataset.var)
          );
          getMeasVars().forEach(v => { if (!existingMeas.has(v)) addMeasItem(el, v); });
        });
      }
      tbConfigPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      tbPreprocError.textContent = err.message;
      tbPreprocError.style.display = 'block';
    } finally {
      tbPreprocBtn.disabled = false;
      tbPreprocBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 17l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Apply Preprocessing on Selected Variables`;
      hideLoading();
    }
  });

  // ── Weight Diagnostics ───────────────────────────────────────────────────
  tbDiagBtn.addEventListener('click', async () => {
    tbDiagBtn.disabled = true;
    showLoading('Running weight diagnostics…');
    try {
      const res  = await fetch('/table-builder/weight-diagnostics', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ weight_column: tbWeightCol, data_source: getDataSrc() }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Diagnostics failed.');
      renderDiagnostics(data.diagnostics, data.weight_column);
      tbDiagPanel.style.display = 'block';
    } catch (err) {
      tbDiagPanel.innerHTML = `<div class="error-msg">${esc(err.message)}</div>`;
      tbDiagPanel.style.display = 'block';
    } finally {
      tbDiagBtn.disabled = false;
      hideLoading();
    }
  });

  function renderDiagnostics(d, wcol) {
    const alertHtml = (d.alerts || []).map(a => {
      const cls = a.level === 'error' ? 'diag-alert-error'
                : a.level === 'warn'  ? 'diag-alert-warn'
                : a.level === 'ok'    ? 'diag-alert-ok'
                : 'diag-alert-info';
      const icon = a.level === 'error' ? '✖' : a.level === 'warn' ? '⚠' : a.level === 'ok' ? '✔' : 'ℹ';
      return `<div class="tb-diag-alert ${cls}">${icon} ${esc(a.msg)}</div>`;
    }).join('');

    const pct = d.percentiles || {};
    tbDiagPanel.innerHTML = `
      <div class="tb-diag-header">Weight Diagnostics — <strong>${esc(wcol)}</strong></div>
      <div class="tb-diag-alerts">${alertHtml}</div>
      <div class="tb-diag-grid">
        <div class="tb-diag-card"><div class="tb-diag-val">${(d.n_valid||0).toLocaleString()}</div><div class="tb-diag-lbl">Valid Weights</div></div>
        <div class="tb-diag-card"><div class="tb-diag-val">${(d.sum||0).toLocaleString()}</div><div class="tb-diag-lbl">Sum of Weights</div></div>
        <div class="tb-diag-card"><div class="tb-diag-val">${d.mean||0}</div><div class="tb-diag-lbl">Mean</div></div>
        <div class="tb-diag-card"><div class="tb-diag-val">${d.median||0}</div><div class="tb-diag-lbl">Median</div></div>
        <div class="tb-diag-card"><div class="tb-diag-val">${d.min||0}</div><div class="tb-diag-lbl">Min</div></div>
        <div class="tb-diag-card"><div class="tb-diag-val">${d.max||0}</div><div class="tb-diag-lbl">Max</div></div>
        <div class="tb-diag-card"><div class="tb-diag-val">${d.cv_pct||0}%</div><div class="tb-diag-lbl">CV (Coeff. of Variation)</div></div>
        <div class="tb-diag-card${d.extreme_count > 0 ? ' diag-card-warn' : ''}"><div class="tb-diag-val">${d.extreme_count||0}</div><div class="tb-diag-lbl">Extreme Weights</div></div>
      </div>
      <div class="tb-diag-pct-row">
        <span class="tb-diag-pct-lbl">Percentiles:</span>
        ${['p1','p5','p25','p50','p75','p95','p99'].map(p =>
          `<span class="tb-diag-pct-chip"><span class="tb-diag-pct-key">${p}</span>${pct[p]||0}</span>`).join('')}
      </div>
    `;
  }

  // ── Table Definitions ────────────────────────────────────────────────────
  tbAddTableBtn.addEventListener('click', addTableDef);

  function getSelectedVars() {
    return tbProcessedVars.length > 0 ? tbProcessedVars : Array.from(tbSelectedCols);
  }
  function getDimVars()  { return getSelectedVars().filter(v => tbVarRoles[v] === 'dimension'); }
  function getMeasVars() { return getSelectedVars().filter(v => tbVarRoles[v] === 'measure'); }
  function getAllVars()   { return getSelectedVars(); }

  function addTableDef() {
    tbTableCounter++;
    const id = tbTableCounter;

    const wrap = document.createElement('div');
    wrap.className = 'tb-table-card';
    wrap.dataset.tableId = id;

    wrap.innerHTML = `
      <div class="tb-table-card-header">
        <span class="tb-table-card-title">Table Definition ${id}</span>
        <button class="tb-remove-btn" title="Remove table">✕</button>
      </div>
      <div class="tb-table-body">

        <!-- Metadata -->
        <div class="tb-card-section">
          <div class="tb-card-section-title">Table Metadata</div>
          <div class="tb-meta-grid">
            <div class="tb-field-col">
              <label class="tb-field-label">Title <span class="tb-required">*</span></label>
              <input type="text" class="tb-title-input tb-name-input" value="Table ${id}" placeholder="e.g. Distribution of Households by State and Sector" />
            </div>
            <div class="tb-field-col">
              <label class="tb-field-label">Universe</label>
              <input type="text" class="tb-universe-input tb-name-input" placeholder="e.g. All sample households" />
            </div>
            <div class="tb-field-col tb-field-col-full">
              <label class="tb-field-label">Notes <span class="tb-optional">(optional)</span></label>
              <input type="text" class="tb-notes-input tb-name-input" placeholder="Source, reference period, or methodology notes" />
            </div>
          </div>
        </div>

        <!-- Filters -->
        <div class="tb-card-section">
          <div class="tb-card-section-header">
            <div class="tb-card-section-title">Filters <span class="tb-optional">(optional)</span></div>
            <button class="tb-add-filter-btn tb-sel-btn">+ Add Filter</button>
          </div>
          <div class="tb-filters-list"></div>
        </div>

        <!-- Dimensions -->
        <div class="tb-card-section">
          <div class="tb-card-section-header">
            <div class="tb-card-section-title">Row Dimensions <span class="tb-required">*</span>
              <span class="tb-optional">(ordered — first = outermost grouping)</span>
            </div>
            <div style="display:flex;gap:6px;align-items:center;">
              <select class="tb-dim-add-sel filter-select" style="font-size:12px;"><option value="">Add dimension variable…</option></select>
              <button class="tb-dim-add-btn tb-sel-btn">+ Add</button>
            </div>
          </div>
          <div class="tb-dims-list"></div>
        </div>

        <!-- Measures -->
        <div class="tb-card-section">
          <div class="tb-card-section-header">
            <div class="tb-card-section-title">Measure Variables <span class="tb-required">*</span>
              <span class="tb-optional">(multiple allowed)</span>
            </div>
            <div style="display:flex;gap:6px;align-items:center;">
              <select class="tb-meas-add-sel filter-select" style="font-size:12px;"><option value="">Add measure variable…</option></select>
              <button class="tb-meas-add-btn tb-sel-btn">+ Add</button>
            </div>
          </div>
          <div class="tb-measures-list"></div>
        </div>

      </div>
    `;

    // Remove table
    wrap.querySelector('.tb-remove-btn').addEventListener('click', () => {
      wrap.remove();
      tbTableDefs = tbTableDefs.filter(t => t.id !== id);
    });

    // Filters
    wrap.querySelector('.tb-add-filter-btn').addEventListener('click', () => addFilterRow(wrap));

    // Dimensions
    const dimAddBtn = wrap.querySelector('.tb-dim-add-btn');
    const dimAddSel = wrap.querySelector('.tb-dim-add-sel');
    dimAddBtn.addEventListener('click', () => {
      if (dimAddSel.value) addDimItem(wrap, dimAddSel.value);
      dimAddSel.value = '';
    });

    // Measures
    const measAddBtn = wrap.querySelector('.tb-meas-add-btn');
    const measAddSel = wrap.querySelector('.tb-meas-add-sel');
    measAddBtn.addEventListener('click', () => {
      if (measAddSel.value) addMeasItem(wrap, measAddSel.value);
      measAddSel.value = '';
    });

    tbTablesContainer.appendChild(wrap);
    tbTableDefs.push({ id, el: wrap });
    populateTableDefDropdowns(wrap);
    // Auto-add any variables already assigned as Dimension/Measure roles
    getDimVars().forEach(v => addDimItem(wrap, v));
    getMeasVars().forEach(v => addMeasItem(wrap, v));
    document.getElementById('tb-generate-wrap').style.display = 'block';
  }

  function addFilterRow(card) {
    const list = card.querySelector('.tb-filters-list');
    const row  = document.createElement('div');
    row.className = 'tb-filter-row';
    const vars = getAllVars();
    const varOpts = vars.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    row.innerHTML = `
      <select class="tb-filter-var filter-select" style="font-size:12px;">${varOpts}</select>
      <select class="tb-filter-op filter-select" style="font-size:12px;width:60px;">
        <option value="==">=</option>
        <option value="!=">≠</option>
        <option value=">">&gt;</option>
        <option value=">=">&ge;</option>
        <option value="<">&lt;</option>
        <option value="<=">&le;</option>
      </select>
      <input type="text" class="tb-filter-val tb-name-input" placeholder="value" style="max-width:120px;" />
      <button class="tb-remove-btn" style="font-size:12px;">✕</button>
    `;
    row.querySelector('.tb-remove-btn').addEventListener('click', () => row.remove());
    list.appendChild(row);
  }

  function addDimItem(card, varName) {
    const list  = card.querySelector('.tb-dims-list');
    const level = list.children.length + 1;
    const item  = document.createElement('div');
    item.className = 'tb-dim-item';
    item.dataset.var = varName;
    item.innerHTML = `
      <span class="tb-dim-tag">${esc(varName)}</span>
      <span class="tb-dim-level-badge">Level ${level}</span>
      <div class="tb-dim-actions">
        <button class="tb-dim-move tb-dim-up" title="Move up">↑</button>
        <button class="tb-dim-move tb-dim-down" title="Move down">↓</button>
        <button class="tb-remove-btn" style="font-size:12px;">✕</button>
      </div>
    `;
    item.querySelector('.tb-dim-up').addEventListener('click', () => {
      if (item.previousElementSibling) list.insertBefore(item, item.previousElementSibling);
      refreshDimLevels(list);
    });
    item.querySelector('.tb-dim-down').addEventListener('click', () => {
      if (item.nextElementSibling) list.insertBefore(item.nextElementSibling, item);
      refreshDimLevels(list);
    });
    item.querySelector('.tb-remove-btn').addEventListener('click', () => { item.remove(); refreshDimLevels(list); });
    list.appendChild(item);
    refreshDimLevels(list);
  }

  function refreshDimLevels(list) {
    Array.from(list.children).forEach((item, i) => {
      const badge = item.querySelector('.tb-dim-level-badge');
      if (badge) badge.textContent = `Level ${i + 1}`;
    });
  }

  function addMeasItem(card, varName) {
    const list = card.querySelector('.tb-measures-list');
    const meta = tbVarMeta[varName] || {};
    const isCat = meta.category && meta.category !== 'Continuous';
    const item  = document.createElement('div');
    item.className = 'tb-meas-item';
    item.dataset.var = varName;

    item.innerHTML = `
      <div class="tb-meas-header">
        <span class="tb-meas-varname">${esc(varName)}</span>
        <span class="tb-cat-badge ${isCat ? 'cat-cat-num' : 'cat-continuous'}" style="margin-left:6px;">${esc(meta.category || 'Continuous')}</span>
        <button class="tb-remove-btn" style="margin-left:auto;font-size:12px;">✕</button>
      </div>
      <div class="tb-meas-config">
        <div class="tb-meas-row">
          <label class="tb-meas-lbl">Display Label</label>
          <input type="text" class="tb-meas-label tb-name-input" value="${esc(varName)}" placeholder="Label for table column header" />
        </div>
        <div class="tb-meas-row">
          <label class="tb-meas-lbl">Estimation Type</label>
          <select class="tb-meas-estim filter-select">
            <option value="Estimated Total">Estimated Total — Σ(W × X)</option>
            <option value="Weighted Percentage" ${isCat ? 'selected' : ''}>Weighted Percentage — Σ(W × I[X=k]) / Σ(W) × 100</option>
            <option value="Weighted Mean" ${!isCat ? 'selected' : ''}>Weighted Mean — Σ(W × X) / Σ(W)</option>
            <option value="Weighted Ratio">Weighted Ratio — Σ(W × Num) / Σ(W × Den)</option>
          </select>
        </div>
        <div class="tb-meas-row tb-ind-cat-row" style="${isCat ? '' : 'display:none;'}">
          <label class="tb-meas-lbl">Indicator Category <span class="tb-optional">I[X=k]</span></label>
          <input type="text" class="tb-meas-ind-cat tb-name-input" placeholder="e.g. 1, Rural, Hindu…" style="max-width:180px;" />
          <span class="tb-optional" style="font-size:11px;">Leave blank for all-categories proportion</span>
        </div>
        <div class="tb-meas-row tb-ratio-den-row" style="display:none;">
          <label class="tb-meas-lbl">Ratio Denominator <span class="tb-required">*</span></label>
          <select class="tb-meas-ratio-den filter-select"></select>
        </div>
      </div>
    `;

    const estimSel    = item.querySelector('.tb-meas-estim');
    const indCatRow   = item.querySelector('.tb-ind-cat-row');
    const ratioDenRow = item.querySelector('.tb-ratio-den-row');
    const ratioDenSel = item.querySelector('.tb-meas-ratio-den');

    // Populate ratio denominator with all vars
    getAllVars().forEach(v => ratioDenSel.appendChild(new Option(v, v)));

    estimSel.addEventListener('change', () => {
      const et = estimSel.value;
      indCatRow.style.display   = et === 'Weighted Percentage' ? '' : 'none';
      ratioDenRow.style.display = et === 'Weighted Ratio'      ? '' : 'none';
    });

    item.querySelector('.tb-remove-btn').addEventListener('click', () => item.remove());
    list.appendChild(item);
  }

  function populateTableDefDropdowns(card) {
    const dimVars  = getDimVars();
    const measVars = getMeasVars();
    const allVars  = getAllVars();

    // Dimension add selector
    const dimSel = card.querySelector('.tb-dim-add-sel');
    const curDim = dimSel.value;
    dimSel.innerHTML = '<option value="">Add dimension variable…</option>';
    dimVars.forEach(v => dimSel.appendChild(new Option(v, v)));
    allVars.filter(v => !dimVars.includes(v) && tbVarRoles[v] !== 'weight').forEach(v =>
      dimSel.appendChild(new Option(`${v} (unassigned)`, v)));
    if (dimVars.includes(curDim) || allVars.includes(curDim)) dimSel.value = curDim;

    // Measure add selector
    const measSel = card.querySelector('.tb-meas-add-sel');
    const curMeas = measSel.value;
    measSel.innerHTML = '<option value="">Add measure variable…</option>';
    measVars.forEach(v => measSel.appendChild(new Option(v, v)));
    allVars.filter(v => !measVars.includes(v) && tbVarRoles[v] !== 'weight').forEach(v =>
      measSel.appendChild(new Option(`${v} (unassigned)`, v)));
    if (measVars.includes(curMeas) || allVars.includes(curMeas)) measSel.value = curMeas;
  }

  function refreshTableDefDropdowns() {
    tbTableDefs.forEach(t => populateTableDefDropdowns(t.el));
  }

  // ── Collect table definitions from DOM ───────────────────────────────────
  function collectTableDefs() {
    return tbTableDefs.map(({ id, el }) => {
      const title    = el.querySelector('.tb-title-input').value.trim() || `Table ${id}`;
      const universe = el.querySelector('.tb-universe-input').value.trim();
      const notes    = el.querySelector('.tb-notes-input').value.trim();

      // Filters
      const filters = Array.from(el.querySelectorAll('.tb-filter-row')).map(row => ({
        variable: row.querySelector('.tb-filter-var').value,
        operator: row.querySelector('.tb-filter-op').value,
        value:    row.querySelector('.tb-filter-val').value.trim(),
      })).filter(f => f.variable && f.value);

      // Dimensions
      const dimensions = Array.from(el.querySelectorAll('.tb-dim-item')).map((item, i) => ({
        variable: item.dataset.var,
        label:    item.dataset.var,
        level:    i + 1,
      }));

      // Measures
      const measures = Array.from(el.querySelectorAll('.tb-meas-item')).map(item => {
        const estimType = item.querySelector('.tb-meas-estim').value;
        return {
          variable:          item.dataset.var,
          label:             item.querySelector('.tb-meas-label').value.trim() || item.dataset.var,
          estimation_type:   estimType,
          indicator_category: estimType === 'Weighted Percentage'
                              ? (item.querySelector('.tb-meas-ind-cat').value.trim() || null) : null,
          ratio_denominator:  estimType === 'Weighted Ratio'
                              ? (item.querySelector('.tb-meas-ratio-den').value || null) : null,
        };
      });

      // Use the creation-time unique ID (not positional index) to prevent collisions
      // when re-generating tables after adding new ones.
      return {
        table_id:   `t${String(id).padStart(3, '0')}`,
        title,
        universe,
        notes,
        filters,
        dimensions,
        measures,
      };
    });
  }

  // ── Generate all tables ──────────────────────────────────────────────────
  tbGenerateBtn.addEventListener('click', async () => {
    tbGenerateError.style.display = 'none';
    const tableDefs = collectTableDefs();

    if (tableDefs.length === 0) {
      tbGenerateError.textContent = 'No table definitions added. Click "Add Table Definition" first.';
      tbGenerateError.style.display = 'block';
      return;
    }

    // Validate
    let valid = true;
    for (const td of tableDefs) {
      if (td.dimensions.length === 0) {
        tbGenerateError.textContent = `"${td.title}": at least one Dimension variable is required.`;
        tbGenerateError.style.display = 'block';
        valid = false; break;
      }
      for (const mc of td.measures) {
        if (mc.estimation_type === 'Weighted Ratio' && !mc.ratio_denominator) {
          tbGenerateError.textContent = `"${td.title}" — measure "${mc.variable}": Ratio Denominator is required for Weighted Ratio.`;
          tbGenerateError.style.display = 'block';
          valid = false; break;
        }
      }
      if (!valid) break;
    }
    if (!valid) return;

    const weightCol   = tbGenWeightCol.value || tbWeightCol;
    const weightLabel = tbGenWeightLabel.value.trim();

    if (!weightCol) {
      tbGenerateError.textContent = 'No weight variable selected. Please select a weight variable above.';
      tbGenerateError.style.display = 'block';
      return;
    }

    tbGenerateBtn.disabled = true;
    showLoading(`Generating ${tableDefs.length} survey table(s)…`);

    try {
      const res  = await fetch('/table-builder/generate-tables', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          weight_variable:       weightCol,
          weight_variable_label: weightLabel,
          data_source:           getDataSrc(),
          tables:                tableDefs,
        }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Generation failed.');

      renderResults(data.tables || []);
      tbResultsPanel.style.display = 'block';
      tbResultsPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      tbGenerateError.textContent = err.message;
      tbGenerateError.style.display = 'block';
    } finally {
      tbGenerateBtn.disabled = false;
      tbGenerateBtn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Generate All Tables`;
      hideLoading();
    }
  });

  // ── Render report-ready results ──────────────────────────────────────────
  function renderResults(tables) {
    tbResultsCont.innerHTML = '';

    tables.forEach((t, idx) => {
      const wrap = document.createElement('div');
      wrap.className = 'tb-result-card';

      if (t.error) {
        wrap.innerHTML = `
          <div class="tb-result-header">
            <span class="tb-result-title">${esc(t.title || t.table_id || `Table ${idx+1}`)}</span>
            <span class="tb-result-err-badge">Error</span>
          </div>
          <div class="tb-result-error">${esc(t.error)}</div>
        `;
      } else {
        const m    = t.methodology || {};
        const cols = t.columns || [];
        const rows = t.preview || [];
        const rts  = t.row_types || [];

        // Methodology block
        const measHtml = (m.measures || []).map(mc =>
          `<div class="tb-report-measure"><strong>${esc(mc.label)}</strong> — ${esc(mc.estimation_type)}
           <span class="tb-report-formula">${esc(mc.formula || '')}</span>
           ${mc.indicator_category ? `<span class="tb-optional"> I[X=${esc(String(mc.indicator_category))}]</span>` : ''}
           ${mc.ratio_denominator  ? `<span class="tb-optional"> ÷ ${esc(mc.ratio_denominator)}</span>` : ''}
          </div>`
        ).join('');

        const dimHtml = (t.dimensions || []).map(d =>
          `<span class="tb-report-dim">${esc(d.label || d.variable)}</span>`
        ).join(' → ');

        const filtersHtml = (t.filters || []).length > 0
          ? `<div class="tb-report-row"><span class="tb-report-key">Filters:</span>
             ${t.filters.map(f => `<code>${esc(f.variable)} ${esc(f.operator)} ${esc(String(f.value))}</code>`).join(', ')}
             </div>` : '';

        const theadCells = cols.map(c => `<th>${esc(c)}</th>`).join('');
        const tbodyRows  = rows.map((row, ri) => {
          const rt = rts[ri] || 'detail';
          const trCls = rt === 'total' ? ' class="tb-row-total"' : rt === 'subtotal' ? ' class="tb-row-subtotal"' : '';
          const cells = cols.map(c => `<td>${esc(String(row[c] ?? ''))}</td>`).join('');
          return `<tr${trCls}>${cells}</tr>`;
        }).join('');

        const rc = t.relation_check;
        const verdict = rc ? rc.verdict : 'moderate';

        wrap.innerHTML = `
          <div class="tb-result-header">
            <span class="tb-result-title">${esc(t.title)}</span>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
              <span class="tb-result-meta">${t.rows} rows · ${cols.length} columns</span>
              <a class="tb-dl-btn" href="/download/generated_tables/${esc(t.filename)}" download>↓ Download CSV</a>
              <button class="tb-add-report-btn" data-table-id="${esc(t.table_id)}" data-table-title="${esc(t.title)}">＋ Add to Report</button>
              ${verdict === 'weak' ? `<span class="tb-weak-label" title="Weak relation — review before including">⚠ Weak relation</span>` : ''}
            </div>
          </div>
          <div class="tb-report-block">
            ${t.universe ? `<div class="tb-report-row"><span class="tb-report-key">Universe:</span> ${esc(t.universe)}</div>` : ''}
            ${filtersHtml}
            <div class="tb-report-row"><span class="tb-report-key">Row Dimensions:</span> ${dimHtml || '—'}</div>
            <div class="tb-report-row"><span class="tb-report-key">Weight Variable:</span>
              <code>${esc(m.weight_variable || '—')}</code>
              ${m.weight_variable_label ? `<span class="tb-optional">(${esc(m.weight_variable_label)})</span>` : ''}
            </div>
            <div class="tb-report-row"><span class="tb-report-key">Method:</span> ${esc(m.estimation_method || 'Horvitz-Thompson')}</div>
            <div class="tb-report-row"><span class="tb-report-key">Data Source:</span> ${esc(m.data_source || 'processed')}
              ${(m.preprocessing_steps||[]).length ? `<span class="tb-optional">· ${(m.preprocessing_steps||[]).join(', ')}</span>` : ''}
            </div>
            <div class="tb-report-measures-block">${measHtml}</div>
            ${t.notes ? `<div class="tb-report-row tb-report-notes">Notes: ${esc(t.notes)}</div>` : ''}
          </div>
          <div class="tb-result-table-wrap">
            <table class="tb-result-table">
              <thead><tr>${theadCells}</tr></thead>
              <tbody>${tbodyRows}</tbody>
            </table>
          </div>
          ${rows.length < t.rows ? `<div class="tb-result-note">Showing first ${rows.length} of ${t.rows} rows — download CSV for full data</div>` : ''}
          ${(() => {
            const rc = t.relation_check;
            if (!rc) return '';
            const icons = { strong: '✅', moderate: '⚠️', weak: '🔴' };
            const labels = {
              strong:   'Strong relation — suitable for report',
              moderate: 'Moderate relation — use with caution',
              weak:     'Weak relation — not recommended for report',
            };
            const icon  = icons[rc.verdict]  || '⚠️';
            const label = labels[rc.verdict] || rc.verdict;
            const text  = rc.explanation ? esc(rc.explanation) : '';
            return `<div class="tb-relation-banner verdict-${esc(rc.verdict)}">
              <span class="tb-relation-icon">${icon}</span>
              <div class="tb-relation-body">
                <span class="tb-relation-label">${label}</span>
                ${text ? `<span class="tb-relation-text">${text}</span>` : ''}
              </div>
            </div>`;
          })()}
        `;
      }

      tbResultsCont.appendChild(wrap);

      // Wire "Add to Report" button
      const addBtn = wrap.querySelector('.tb-add-report-btn');
      if (addBtn) {
        const tid    = addBtn.dataset.tableId;
        const ttitle = addBtn.dataset.tableTitle;
        // Restore "✓ In Report" state if this table is already in the basket,
        // but keep the button ENABLED so the user can re-add/update it after
        // re-generating the same table definition with different settings.
        if (window._reportBasket && window._reportBasket.has(tid)) {
          addBtn.textContent = '✓ In Report';
          addBtn.classList.add('added');
        }
        addBtn.addEventListener('click', () => {
          window._reportBasket.add(tid, ttitle, t, addBtn);
        });
      }
    });
  }

  // Restore on page load
  window._tbRestoreStatus = async function () {
    try {
      const res  = await fetch('/table-builder/status');
      const data = await res.json();
      if (data.generated && data.generated.length > 0) {
        renderResults(data.generated);
        tbResultsPanel.style.display = 'block';
      }
    } catch (_) {}
  };
  window._tbRestoreStatus();
})();

// ═══════════════════════════════════════════════════════════════════════════
// STEP 6 — REPORT BASKET & AI REPORT GENERATOR
// ═══════════════════════════════════════════════════════════════════════════
(function () {
  'use strict';

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const basketEmpty      = document.getElementById('report-basket-empty');
  const basketPanel      = document.getElementById('report-basket');
  const basketList       = document.getElementById('report-basket-list');
  const basketCount      = document.getElementById('report-basket-count');
  const queryInput       = document.getElementById('report-query');
  const generateBtn      = document.getElementById('report-generate-btn');
  const clearBtn         = document.getElementById('report-clear-btn');
  const generateError    = document.getElementById('report-generate-error');
  const previewPanel     = document.getElementById('report-preview-panel');
  const reportIframe     = document.getElementById('report-iframe');
  const redownloadBtn    = document.getElementById('report-redownload-btn');

  const loadingOverlay   = document.getElementById('loading-overlay');
  const loadingText      = document.getElementById('loading-text');
  function showLoading(t) { loadingText.textContent = t || 'Processing…'; loadingOverlay.style.display = 'flex'; }
  function hideLoading()  { loadingOverlay.style.display = 'none'; }
  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  // ── Basket state ──────────────────────────────────────────────────────────
  // Map: basketKey (b1, b2, …) → { title, tableData, real_id }
  // Each "Add to Report" click gets its own key so multiple versions of the
  // same table slot can coexist in the basket without overwriting each other.
  const basket = new Map();
  let basketCounter      = 0;
  let lastReportHtml     = '';
  let lastReportFilename = '';

  function syncBasketUI() {
    const count = basket.size;
    basketCount.textContent = count;
    if (count === 0) {
      basketPanel.style.display  = 'none';
      basketEmpty.style.display  = 'block';
    } else {
      basketPanel.style.display  = 'block';
      basketEmpty.style.display  = 'none';
    }

    basketList.innerHTML = '';
    basket.forEach((item, tid) => {
      const row = document.createElement('div');
      row.className = 'report-basket-row';
      row.innerHTML = `
        <span class="report-basket-title">${esc(item.title)}</span>
        <button class="report-basket-remove" data-tid="${esc(tid)}" title="Remove from report">✕</button>
      `;
      row.querySelector('.report-basket-remove').addEventListener('click', () => {
        basket.delete(tid);  // tid here is the basket map key (b1, b2, …)
        // Reset the "Add to Report" button only if no other basket entry
        // still references the same real table_id
        const realId = item.real_id;
        const stillReferenced = Array.from(basket.values()).some(v => v.real_id === realId);
        if (!stillReferenced) {
          const btn = document.querySelector(`.tb-add-report-btn[data-table-id="${realId}"]`);
          if (btn) {
            btn.textContent = '＋ Add to Report';
            btn.classList.remove('added');
          }
        }
        syncBasketUI();
      });
      basketList.appendChild(row);
    });
  }

  function markBtnAdded(btnEl) {
    if (!btnEl) return;
    btnEl.textContent = '✓ In Report';
    btnEl.classList.add('added');
  }

  // ── Public API used by table builder ─────────────────────────────────────
  window._reportBasket = {
    add(tid, ttitle, tableData, btnEl) {
      // Always create a NEW basket entry (b1, b2, …) so re-generating the
      // same table slot and clicking "Add to Report" accumulates entries
      // instead of overwriting the previous one.
      basketCounter++;
      basket.set(`b${basketCounter}`, { title: ttitle, tableData, real_id: tid });
      markBtnAdded(btnEl);
      syncBasketUI();
      document.getElementById('report-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    },
    // Returns true if any basket entry references this real table_id.
    has(tid) {
      return Array.from(basket.values()).some(v => v.real_id === tid);
    },
  };

  // ── Clear basket ──────────────────────────────────────────────────────────
  clearBtn.addEventListener('click', () => {
    basket.clear();
    // Reset all "Add to Report" buttons
    document.querySelectorAll('.tb-add-report-btn').forEach(btn => {
      btn.textContent = '＋ Add to Report';
      btn.classList.remove('added');
    });
    syncBasketUI();
  });

  // ── Generate report ──────────────────────────────────────────────────────
  generateBtn.addEventListener('click', async () => {
    generateError.style.display = 'none';
    if (basket.size === 0) {
      generateError.textContent = 'Add at least one table to the report basket first.';
      generateError.style.display = 'block';
      return;
    }

    // Send full table snapshots so the backend uses the exact data that was
    // in the basket at the time of clicking, not whatever happens to be in
    // session_data now (which may have been overwritten by later generations).
    const tables = Array.from(basket.values()).map(v => v.tableData);
    const query  = (queryInput ? queryInput.value || '' : '').trim();

    generateBtn.disabled = true;
    showLoading('Generating report…');

    try {
      const payload = { tables, query };

      const res  = await fetch('/report/generate', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Report generation failed.');

      lastReportHtml     = data.html     || '';
      lastReportFilename = data.filename || 'report.html';

      // Render in iframe (srcdoc keeps it self-contained)
      reportIframe.srcdoc = lastReportHtml;
      previewPanel.style.display = 'block';
      previewPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });

    } catch (err) {
      generateError.textContent = err.message;
      generateError.style.display = 'block';
    } finally {
      generateBtn.disabled = false;
      generateBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Generate AI Report`;
      hideLoading();
    }
  });

  // ── Download: capture any edits made in the iframe, then download ─────────
  redownloadBtn.addEventListener('click', () => {
    if (!lastReportFilename) return;
    // Grab whatever HTML is currently in the iframe (captures edits)
    let html = lastReportHtml;
    try {
      const iDoc = reportIframe.contentDocument || reportIframe.contentWindow.document;
      if (iDoc) html = iDoc.documentElement.outerHTML;
    } catch (_) { /* cross-origin guard; use server copy */ }

    const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = lastReportFilename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  // Init
  syncBasketUI();
})();
