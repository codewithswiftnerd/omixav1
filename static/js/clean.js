const $ = (id) => document.getElementById(id);
const drop = $('drop'), fileInput = $('fileInput'), fileNameEl = $('fileName');
const runBtn = $('runBtn'), downloadBtn = $('downloadBtn'), resetBtn = $('resetBtn');
const stage = $('stage'), results = $('results'), stats = $('stats'), detailBlocks = $('detailBlocks');
const reportCard = $('reportCard'), scoreBadge = $('scoreBadge'), scoreNum = $('scoreNum');
const reportSub = $('reportSub'), findingsEl = $('findings'), noFindings = $('noFindings');
const recommendationsCard = $('recommendationsCard');
const recSafeList = $('recSafeList'), recSafeEmpty = $('recSafeEmpty');
const recAmbiguousList = $('recAmbiguousList'), recAmbiguousEmpty = $('recAmbiguousEmpty');
const hasHeaderCheck = $('hasHeaderCheck');
const workCard = $('workCard'), uploadCard = $('uploadCard');
const changeFileBtn = $('changeFileBtn'), reportTitle = $('reportTitle');

// The home page is a single-screen tool: while a file is loaded the
// hero/upload card/marketing sections are hidden (body.has-file) and
// workCard moves through phases that CSS keys off:
//   idle -> working (upload + analysis) -> review -> done  (error -> review/idle)
function setPhase(phase){
  if(workCard) workCard.dataset.phase = phase;
}
function showWorkspace(on){
  document.body.classList.toggle('has-file', on);
  if(workCard) workCard.classList.toggle('hidden', !on);
  if(uploadCard) uploadCard.classList.toggle('hidden', on);
  if(on) window.scrollTo({ top: 0, behavior: 'smooth' });
}

function hasHeader(){
  return hasHeaderCheck ? hasHeaderCheck.checked : true;
}

// Mirrors the titles already in the static rule checklist below, so a
// recommendation card and its underlying checkbox always agree on
// what a rule is called.
const RULE_TITLES = {
  column_names: 'Standardize column names',
  formatting: 'Fix formatting',
  missing_token_normalization: 'Recognize blank/N-A style values',
  numeric_text_cleaning: 'Clean numeric text',
  gender_standardization: 'Standardize gender',
  country_standardization: 'Standardize countries',
  boolean_standardization: 'Standardize booleans',
  categorical_standardization: 'Standardize categories',
  email_cleaning: 'Clean emails',
  phone_cleaning: 'Clean phone formatting',
  date_standardization: 'Standardize dates',
  missing_values: 'Fill gaps',
  duplicates: 'Remove duplicates',
};

function ruleCheckbox(ruleId){
  return document.querySelector(`.rule input[value="${CSS.escape(ruleId)}"]`);
}
const stepEls = {
  upload: document.querySelector('.step[data-step="upload"]'),
  review: document.querySelector('.step[data-step="review"]'),
  clean: document.querySelector('.step[data-step="clean"]'),
  download: document.querySelector('.step[data-step="download"]'),
};

let selectedFile = null;
let lastJobId = null;
let preCleanScore = null;

function base(){
  const configured = (window.OMIXA_API_BASE || '').trim().replace(/\/$/, '');
  return configured || window.location.origin;
}

// Auth is the browser's session cookie (sent automatically via
// credentials:'include' on every fetch below) — nothing to attach
// here. Kept as a passthrough so call sites don't need to change.
function apiHeaders(extra){
  return extra || {};
}

function setStep(name, state){
  // state: 'active' | 'done' | 'err' | '' (reset)
  Object.values(stepEls).forEach(el => el && el.classList.remove('active','done','err'));
  const order = ['upload','review','clean','download'];
  const idx = order.indexOf(name);
  order.forEach((key, i) => {
    const el = stepEls[key];
    if(!el) return;
    if(i < idx) el.classList.add('done');
    if(i === idx && state) el.classList.add(state);
  });
}

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); fileInput.click(); } });
['dragover','dragenter'].forEach(evt => drop.addEventListener(evt, e => { e.preventDefault(); drop.classList.add('drag'); }));
['dragleave','drop'].forEach(evt => drop.addEventListener(evt, e => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', e => { if(e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', e => { if(e.target.files[0]) setFile(e.target.files[0]); });
if(hasHeaderCheck){
  hasHeaderCheck.addEventListener('change', () => { if(selectedFile) setFile(selectedFile); });
}

// Queue mode: the API answers 202 and a dedicated worker does the heavy work, so the page polls
// the job instead of holding one long request open. Polling backs off (1s -> 5s) so thousands of
// open tabs stay cheap for the server, and obeys Retry-After if rate limited.
const sleep = ms => new Promise(res => setTimeout(res, ms));
async function pollJob(jobId, isDone){
  const started = Date.now();
  let delay = 1000;
  for(;;){
    const r = await fetch(base() + '/api/jobs/' + jobId, { credentials:'include', headers: apiHeaders() });
    if(r.status === 429){
      await sleep((parseInt(r.headers.get('Retry-After') || '3', 10) || 3) * 1000);
      continue;
    }
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || 'status check failed');
    if(j.status === 'failed') throw new Error(j.error || 'processing failed');
    if(isDone(j)) return j;
    if(j.status === 'queued'){
      setStage(j.queue_position ? 'Waiting in queue (position ' + j.queue_position + ') …' : 'Queued …', 'active');
    }
    if(Date.now() - started > 15 * 60 * 1000) throw new Error('This is taking longer than expected. Please try again.');
    await sleep(delay);
    delay = Math.min(Math.round(delay * 1.4), 5000);
  }
}

async function setFile(f){
  selectedFile = f;
  lastJobId = null;
  preCleanScore = null;
  fileNameEl.textContent = f.name + '  ·  ' + (f.size >= 1048576 ? (f.size/1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(f.size/1024)) + ' KB');
  showWorkspace(true);
  setPhase('working');
  runBtn.disabled = true;
  reportCard.classList.add('hidden');
  recommendationsCard.classList.add('hidden');
  results.classList.add('hidden');
  downloadBtn.classList.add('hidden'); resetBtn.classList.add('hidden');
  setStage('');

  try{
    setStep('upload', 'active');
    setStage('Uploading your file …', 'active');
    const form = new FormData();
    form.append('file', f);
    const upRes = await fetch(base() + '/api/upload/', { method:'POST', credentials:'include', headers: apiHeaders(), body: form });
    const upJson = await upRes.json();
    if(!upRes.ok) throw upgradeAwareError(upRes, upJson, 'upload failed');
    lastJobId = upJson.job_id;

    setStep('review', 'active');
    setStage('Checking your data …', 'active');
    const repRes = await fetch(base() + '/api/report/' + lastJobId + '?has_header=' + hasHeader(), { credentials:'include', headers: apiHeaders() });
    let repJson = await repRes.json();
    if(!repRes.ok) throw upgradeAwareError(repRes, repJson, 'analysis failed');
    if(repRes.status === 202){
      if(repJson.message) setStage(repJson.message, 'active');
      const wantHeader = hasHeader();
      const done = await pollJob(lastJobId, j => j.status === 'uploaded' && j.analysis && j.analysis.has_header === wantHeader);
      repJson = { report: done.analysis.report, recommendations: done.analysis.recommendations };
    }

    renderReport(repJson.report);
    renderRecommendations(repJson.recommendations);
    setStage('');
    setPhase('review');
    runBtn.disabled = false;
  }catch(err){
    setStage(err.message, 'error');
    setStep(lastJobId ? 'review' : 'upload', 'err');
    setPhase('error');
  }
}

// Wraps a failed response's error message so a 402 (plan limit / file
// too large, see plans.py) also surfaces a link to upgrade, instead
// of just reading like any other error.
function upgradeAwareError(res, json, fallback){
  const msg = json.error || fallback;
  if(res.status === 402 && json.upgrade_required && window.OMIXA_UPGRADE_URL){
    return new Error(msg + ', upgrade at ' + window.OMIXA_UPGRADE_URL);
  }
  return new Error(msg);
}

// Every value that originates from the uploaded file (column names, sample
// values, free-text details) must be escaped before it is placed in innerHTML.
function escHtml(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 };

function renderReport(report){
  preCleanScore = report.score;
  reportCard.classList.remove('hidden');
  scoreNum.innerHTML = escHtml(report.score) + (report.grade ? '<small>' + escHtml(report.grade) + '</small>' : '');
  scoreBadge.className = 'score-badge ' + (report.score >= 85 ? 'good' : report.score >= 60 ? 'warn' : 'bad');
  const c = report.counts || {};
  const total = (c.critical || 0) + (c.warning || 0) + (c.info || 0);
  reportTitle.textContent = total === 0
    ? 'Looks clean already'
    : total === 1 ? 'We found 1 thing to fix' : `We found ${total} things to fix`;
  reportSub.textContent = `${Number(report.row_count).toLocaleString()} rows · ${report.column_count} columns`
    + (c.critical ? ` · ${c.critical} critical` : '');

  const findings = (report.findings || []).slice().sort((a,b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
  findingsEl.innerHTML = '';
  noFindings.style.display = findings.length ? 'none' : 'block';

  findings.forEach(f => {
    const row = document.createElement('div');
    row.className = 'finding finding-' + f.severity;
    row.innerHTML = `
      <span class="badge ${escHtml(f.severity)}">${escHtml(f.severity)}</span>
      <div class="finding-body">
        <strong>${f.column ? escHtml(f.column) : 'Whole file'}</strong>
        <p>${escHtml(f.detail)}</p>
        <p class="suggestion">${escHtml(f.suggestion)}</p>
      </div>`;
    findingsEl.appendChild(row);
  });
}

// Renders the Recommendations panel from { safe, ambiguous,
// recommended_rules } (see cleaning/recommendations.py). This never
// changes which rules exist or their default-checked state in the
// .rules list below, it only surfaces, per-issue, which of those
// already-available rules the current file's analysis backs up, and
// lets the user toggle a recommendation to include/exclude that rule
// from the run. Findings with no safe rule (or "critical" severity)
// show up as read-only "needs review" items instead of a checkbox,
// since there's no automated fix to opt in or out of.
function renderRecommendations(recommendations){
  if(!recommendations){ recommendationsCard.classList.add('hidden'); return; }
  recommendationsCard.classList.remove('hidden');

  const safe = recommendations.safe || [];
  const ambiguous = recommendations.ambiguous || [];
  if(ambiguous.length){
    reportSub.textContent += ` · ${ambiguous.length} need${ambiguous.length === 1 ? 's' : ''} your review (open Customize)`;
  }

  recSafeList.innerHTML = '';
  recSafeEmpty.style.display = safe.length ? 'none' : 'block';
  safe.forEach(rec => {
    const checkbox = ruleCheckbox(rec.rule);
    const title = RULE_TITLES[rec.rule] || rec.rule;
    const evidence = rec.findings.map(f => (f.column ? f.column + ': ' : '') + f.detail).join(' · ');

    const item = document.createElement('label');
    item.className = 'rec-item safe';
    item.innerHTML = `
      <input type="checkbox" ${checkbox && checkbox.checked ? 'checked' : ''}>
      <div class="rec-item-body">
        <strong>${escHtml(title)}</strong>
        <p>${escHtml(rec.reason)}</p>
        <p class="rec-evidence">${escHtml(evidence)}</p>
      </div>`;
    const input = item.querySelector('input');
    // Two-way sync: this checkbox is a view onto the real rule
    // checkbox in .rules, so unchecking a recommendation actually
    // excludes that rule from the /api/process request, and vice
    // versa, nothing new to wire into selectedRules().
    input.addEventListener('change', () => { if(checkbox) checkbox.checked = input.checked; });
    if(checkbox) checkbox.addEventListener('change', () => { input.checked = checkbox.checked; });
    recSafeList.appendChild(item);
  });

  recAmbiguousList.innerHTML = '';
  recAmbiguousEmpty.style.display = ambiguous.length ? 'none' : 'block';
  ambiguous
    .slice()
    .sort((a,b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
    .forEach(f => {
      const item = document.createElement('div');
      item.className = 'rec-item';
      const sampleHtml = f.sample_values && f.sample_values.length
        ? `<p class="rec-evidence">e.g. ${escHtml(f.sample_values.join(', '))}</p>` : '';

      const options = f.resolution_options || [];
      let choiceHtml = '';
      if(options.length){
        const selectId = 'resolve-' + Math.random().toString(36).slice(2);
        const hasGroups = options.some(o => o.group);
        let opts;
        if(hasGroups){
          // Grouped (currently: phone country codes by region) so a
          // long list like "suspicious_phone_format"'s ~30 countries
          // doesn't read as one undifferentiated wall of options.
          const byGroup = {};
          options.forEach(o => { (byGroup[o.group] = byGroup[o.group] || []).push(o); });
          opts = Object.entries(byGroup).map(([group, opts]) =>
            `<optgroup label="${escHtml(group)}">` +
            opts.map(o => `<option value="${escHtml(o.id)}">${escHtml(o.label)}</option>`).join('') +
            `</optgroup>`
          ).join('');
        } else {
          opts = options.map(o => `<option value="${escHtml(o.id)}">${escHtml(o.label)}</option>`).join('');
        }
        choiceHtml = `
          <select class="resolve-select" id="${selectId}"
                  data-column="${escHtml(f.column || '')}" data-issue="${escHtml(f.issue)}">
            <option value="skip" selected>Leave as is (no change)</option>${opts}
          </select>`;
      }

      item.innerHTML = `
        <span class="badge ${escHtml(f.severity)}">${escHtml(f.severity)}</span>
        <div class="rec-item-body">
          <strong>${f.column ? escHtml(f.column) : 'Whole file'}</strong>
          <p>${escHtml(f.detail)}</p>
          ${sampleHtml}
          <p class="rec-evidence">${escHtml(f.suggestion)}</p>
          ${choiceHtml}
        </div>`;
      recAmbiguousList.appendChild(item);
    });
}

// Reads every resolve-select the user touched (left on the "skip"
// default is the same as not sending it at all, so those are
// filtered out, the backend's default behavior is already "leave
// as is" for anything it doesn't receive).
function selectedResolutions(){
  return Array.from(document.querySelectorAll('.resolve-select'))
    .filter(sel => sel.value !== 'skip')
    .map(sel => ({ column: sel.dataset.column, issue: sel.dataset.issue, choice: sel.value }));
}

function setStage(text, cls){
  stage.textContent = text;
  stage.className = 'stage' + (cls ? ' ' + cls : '');
}

function selectedRules(){
  // Only the rule checklist: the header toggle also lives in a .rule label.
  return Array.from(document.querySelectorAll('fieldset.rules .rule input:checked')).map(c => c.value);
}

runBtn.addEventListener('click', async () => {
  if(!selectedFile || !lastJobId) return;
  runBtn.disabled = true;
  results.classList.add('hidden');
  downloadBtn.classList.add('hidden');
  try{
    setStep('clean', 'active');
    setPhase('working');
    const rules = selectedRules();
    setStage('Cleaning your data …', 'active');
    const procRes = await fetch(base() + '/api/process/' + lastJobId, {
      method:'POST',
      credentials:'include',
      headers: apiHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({ rules, resolutions: selectedResolutions(), has_header: hasHeader(), profile_id: (window.OmixaPro && OmixaPro.profileId()) || undefined })
    });
    let procJson = await procRes.json();
    if(!procRes.ok) throw upgradeAwareError(procRes, procJson, 'processing failed');
    if(procRes.status === 202){
      if(procJson.message) setStage(procJson.message, 'active');
      const done = await pollJob(lastJobId, j => j.status === 'processed' && j.summary);
      procJson = { summary: done.summary };
    }

    setStep('download', '');
    setStage('');
    setPhase('done');
    renderResults(procJson.summary);
    if(window.OmixaPro) OmixaPro.showResults(procJson.summary);
    downloadBtn.classList.remove('hidden');
    resetBtn.classList.remove('hidden');
  }catch(err){
    setStage(err.message, 'error');
    setStep(lastJobId ? 'clean' : 'upload', 'err');
    setPhase(lastJobId ? 'review' : 'error');
  }finally{
    runBtn.disabled = false;
  }
});

function renderResults(summary){
  results.classList.remove('hidden');
  const c = summary.changes || {};

  const post = summary.quality_report;
  const beforeGrade = summary.quality_report_before ? summary.quality_report_before.grade : null;
  let scoreHtml = '';
  if(post){
    const cls = post.score >= 85 ? 'good' : post.score >= 60 ? 'warn' : 'bad';
    const before = preCleanScore != null ? preCleanScore : post.score;
    const delta = post.score - before;
    const deltaText = delta > 0 ? `+${delta}` : delta === 0 ? 'no change' : `${delta}`;
    const badge = (score, grade) => escHtml(score) + (grade ? '<small>' + escHtml(grade) + '</small>' : '');
    const beforeLabel = badge(before, beforeGrade);
    const afterLabel = badge(post.score, post.grade);
    scoreHtml = `
      <div class="score-compare">
        <div class="score-compare-item">
          <div class="score-badge small ${before >= 85 ? 'good' : before >= 60 ? 'warn' : 'bad'}">${beforeLabel}</div>
          <div class="l">before</div>
        </div>
        <span class="score-arrow">→</span>
        <div class="score-compare-item">
          <div class="score-badge small ${cls}">${afterLabel}</div>
          <div class="l">after (${deltaText})</div>
        </div>
      </div>`;
  }

  stats.innerHTML = scoreHtml + `
    <div class="stat"><div class="n">${summary.rows_in}</div><div class="l">rows in</div></div>
    <div class="stat"><div class="n">${summary.rows_out}</div><div class="l">rows out</div></div>
  `;
  // Only list the changes that actually happened, so the summary stays short.
  [['formatting_changed','cells reformatted'],['missing_values_changed','values filled'],['duplicates_changed','duplicates removed']]
    .forEach(([key, label]) => {
      if(c[key]) stats.innerHTML += `<div class="stat"><div class="n">${c[key]}</div><div class="l">${label}</div></div>`;
    });


  // Any other "<rule>_changed" counters (the newer standardization
  // rules) get their own small stat cards too, so nothing added to
  // the pipeline later silently fails to show up here.
  const KNOWN_STAT_KEYS = new Set(['formatting_changed','missing_values_changed','duplicates_changed']);
  const STAT_LABELS = {
    column_names_changed: 'headers cleaned',
    missing_token_normalization_changed: 'blanks recognized',
    numeric_text_cleaning_changed: 'numbers cleaned',
    gender_standardization_changed: 'genders standardized',
    country_standardization_changed: 'countries standardized',
    boolean_standardization_changed: 'booleans standardized',
    categorical_standardization_changed: 'categories merged',
    email_cleaning_changed: 'emails normalized',
    phone_cleaning_changed: 'phone values cleaned',
    date_standardization_changed: 'dates standardized',
  };
  Object.entries(c).forEach(([key, val]) => {
    if(KNOWN_STAT_KEYS.has(key) || !val) return;
    const label = STAT_LABELS[key] || key.replace(/_changed$/, '').replace(/_/g, ' ');
    stats.innerHTML += `<div class="stat"><div class="n">${val}</div><div class="l">${label}</div></div>`;
  });

  detailBlocks.innerHTML = '';
  const d = summary.details || {};

  // The spec-style plain-text "DATA CLEANING SUMMARY" (see
  // cleaning/summary.py), shown first, verbatim, so it's easy to
  // copy/paste elsewhere; the tables below break the same numbers
  // down per column for anyone who wants more detail.
  if(summary.cleaning_summary && summary.cleaning_summary.text){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Cleaning summary</h3><pre class="cleaning-summary-text">${escHtml(summary.cleaning_summary.text)}</pre>`;
    detailBlocks.appendChild(block);
  }

  if(d.missing_values){
    const rows = Object.entries(d.missing_values.per_column || {});
    if(rows.length){
      const block = document.createElement('div');
      block.innerHTML = `<h3>Missing values by column</h3>` + tableFrom(
        ['column','action','missing %','filled'],
        rows.map(([col, v]) => [
          col,
          {html: `<span class="badge ${v.action === 'median_imputation' ? 'median' : v.action === 'unknown_category' ? 'unknown' : 'review'}">${escHtml(String(v.action).replace(/_/g,' '))}</span>`},
          v.missing_percentage + '%',
          v.filled ?? '-'
        ])
      );
      detailBlocks.appendChild(block);
    }
  }

  if(d.formatting && Object.keys(d.formatting.per_column_cells_changed || {}).length){
    const rows = Object.entries(d.formatting.per_column_cells_changed);
    const block = document.createElement('div');
    block.innerHTML = `<h3>Formatting by column</h3>` + tableFrom(
      ['column','cells changed'], rows.map(([col, n]) => [col, n])
    );
    detailBlocks.appendChild(block);
  }

  if(d.duplicates){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Duplicates</h3><p class="hint">Found ${d.duplicates.duplicate_rows_found}, removed ${d.duplicates.removed}, kept "${d.duplicates.kept}" occurrence.</p>`;
    detailBlocks.appendChild(block);
  }

  if(d.column_names && Object.keys(d.column_names.renamed || {}).length){
    const rows = Object.entries(d.column_names.renamed);
    const block = document.createElement('div');
    block.innerHTML = `<h3>Column names cleaned up</h3>` + tableFrom(
      ['original', 'cleaned'], rows.map(([oldName, newName]) => [oldName, newName])
    );
    detailBlocks.appendChild(block);
  }

  // Generic renderer for every other per-column standardization rule
  // (numeric text, booleans, categories, emails, phone, dates, missing
  // tokens) so a new rule shows up here automatically without needing
  // a bespoke block; they all share the same { per_column: {...} } shape.
  const GENERIC_DETAIL_TITLES = {
    missing_token_normalization: 'Blank / N-A style values recognized',
    numeric_text_cleaning: 'Numeric text cleaned',
    gender_standardization: 'Gender standardized',
    country_standardization: 'Country standardized',
    boolean_standardization: 'Booleans standardized',
    categorical_standardization: 'Categories standardized',
    email_cleaning: 'Emails cleaned',
    phone_cleaning: 'Phone formatting cleaned',
    date_standardization: 'Dates standardized',
  };
  Object.entries(GENERIC_DETAIL_TITLES).forEach(([ruleName, title]) => {
    const detail = d[ruleName];
    const perColumn = detail && detail.per_column;
    const rows = perColumn ? Object.entries(perColumn) : [];
    if(!rows.length && !(detail && detail.skipped_ambiguous && detail.skipped_ambiguous.length)) return;

    const block = document.createElement('div');
    let html = rows.length ? `<h3>${title}</h3>` + tableFrom(
      ['column', 'cells changed'],
      rows.map(([col, v]) => [col, typeof v === 'object' ? (v.changed ?? '-') : v])
    ) : `<h3>${title}</h3>`;
    if(detail && detail.skipped_ambiguous && detail.skipped_ambiguous.length){
      html += `<p class="hint">Skipped (format too ambiguous to guess safely): ${escHtml(detail.skipped_ambiguous.join(', '))}</p>`;
    }
    block.innerHTML = html;
    detailBlocks.appendChild(block);
  });

  if(post && post.findings && post.findings.length){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Still worth a look</h3>`;
    const list = document.createElement('div');
    list.className = 'findings';
    post.findings
      .slice()
      .sort((a,b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
      .forEach(f => {
        const row = document.createElement('div');
        row.className = 'finding finding-' + f.severity;
        row.innerHTML = `
          <span class="badge ${escHtml(f.severity)}">${escHtml(f.severity)}</span>
          <div class="finding-body">
            <strong>${f.column ? escHtml(f.column) : 'Whole file'}</strong>
            <p>${escHtml(f.detail)}</p>
            <p class="suggestion">${escHtml(f.suggestion)}</p>
          </div>`;
        list.appendChild(row);
      });
    block.appendChild(list);
    detailBlocks.appendChild(block);
  }

  if(summary.resolutions_applied && summary.resolutions_applied.length){
    const block = document.createElement('div');
    block.innerHTML = `<h3>Your resolution choices</h3>` + tableFrom(
      ['column', 'issue', 'choice', 'cells changed'],
      summary.resolutions_applied.map(r => [r.column, r.issue.replace(/_/g,' '), r.choice.replace(/_/g,' '), r.changed])
    );
    detailBlocks.appendChild(block);
  }
}

// Cells are escaped by default. A caller that needs markup (e.g. a badge) must
// opt in explicitly with {html: '...'} and is then responsible for escaping any
// data it interpolates.
function tableFrom(headers, rows){
  const cellHtml = cell => (cell && typeof cell === 'object' && 'html' in cell) ? cell.html : escHtml(cell);
  const th = headers.map(h => `<th>${escHtml(h)}</th>`).join('');
  const tr = rows.map(r => `<tr>${r.map(cell => `<td>${cellHtml(cell)}</td>`).join('')}</tr>`).join('');
  return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

// Saves `blob` to disk under `filename`. On browsers that support the
// File System Access API (desktop Chrome/Edge/Opera), this opens the
// native "Save As" dialog so the user can pick the name and folder,
// same as a normal download in a desktop app. Browsers without that
// API (Firefox, Safari, and ALL mobile browsers - Chrome for Android
// and iOS Safari included - have no such dialog to offer) fall back
// to the classic silent save-to-Downloads behavior; that fallback is
// a real platform limitation, not a bug in this app.
async function saveDownloadedFile(blob, filename){
  if (window.showSaveFilePicker){
    const dotIndex = filename.lastIndexOf('.');
    const ext = dotIndex > -1 ? filename.slice(dotIndex) : '';
    try{
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: ext ? [{
          description: ext.slice(1).toUpperCase() + ' file',
          accept: { [blob.type || 'application/octet-stream']: [ext] }
        }] : undefined
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return true;
    }catch(e){
      if (e.name === 'AbortError') return false; // user cancelled the dialog
      // Fall through to the classic method for any other failure.
    }
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  return true;
}

downloadBtn.addEventListener('click', async () => {
  if(!lastJobId) return;
  let r = await fetch(base() + '/api/download/' + lastJobId + '?format=url', { credentials:'include', headers: apiHeaders() });
  let signedName = null;
  if(r.ok && (r.headers.get('Content-Type') || '').includes('application/json')){
    // Object-storage mode: the file comes straight from a short-lived signed URL (no cookies sent).
    const j = await r.json();
    signedName = j.filename;
    r = await fetch(j.url);
  }
  if(!r.ok){
    let msg = 'download failed (HTTP ' + r.status + ')';
    try{
      const j = await r.json();
      msg = 'download failed: ' + (j.detail || j.error || msg);
    }catch(e){ /* response wasn't JSON, keep the generic message */ }
    setStage(msg, 'error');
    return;
  }
  const blob = await r.blob();
  const cd = r.headers.get('Content-Disposition') || '';
  const match = cd.match(/filename="?([^"]+)"?/);
  const filename = signedName || (match ? match[1] : 'cleaned_output');
  await saveDownloadedFile(blob, filename);
  downloadBtn.classList.add('hidden'); // job is deleted server-side after download
});

function resetAll(){
  selectedFile = null; lastJobId = null; preCleanScore = null;
  fileNameEl.textContent = ''; fileInput.value = '';
  runBtn.disabled = true;
  results.classList.add('hidden');
  reportCard.classList.add('hidden');
  recommendationsCard.classList.add('hidden');
  downloadBtn.classList.add('hidden'); resetBtn.classList.add('hidden');
  setStage('');
  setStep('upload', '');
  setPhase('idle');
  showWorkspace(false);
}
resetBtn.addEventListener('click', resetAll);
if(changeFileBtn) changeFileBtn.addEventListener('click', resetAll);

// "No file? Try one of these": build a File from the bundled sample CSV
// and push it through exactly the same path as a real upload.
document.querySelectorAll('.sample').forEach(btn => {
  btn.addEventListener('click', () => {
    const sample = (window.OMIXA_SAMPLES || {})[btn.dataset.sample];
    if(!sample) return;
    setFile(new File([sample.csv], sample.filename, { type: 'text/csv' }));
  });
});
