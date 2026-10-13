/**
 * OmixaQualityPanel
 *
 * The floating "data-quality control center" (19.09.2026 planning
 * note): a button that floats over the workspace once a file's been
 * analyzed, opening a compact panel with the quality score and a
 * grouped list of actions (duplicates / missing values / invalid
 * fields / standardization), each either a one-click toggle for a
 * rule Omixa can auto-fix, or a jump straight to the matching item
 * in the existing review UI for anything that needs a human call.
 *
 * Deliberately kept as a small, self-contained module that only
 * consumes the exact JSON /api/report/<job_id> already returns
 * (report + recommendations, see cleaning/quality_report.py and
 * cleaning/recommendations.py) plus a handful of DOM-agnostic
 * callbacks - it never calls fetch() itself and never reaches into
 * clean.js's element ids directly. That's on purpose: the web
 * workspace is phase 1 of this control center; a future Chrome/
 * Google Sheets extension or Excel add-in is meant to hit that same
 * public endpoint and feed its JSON straight into this same
 * groupActions()/render() logic, so "what does a quality control
 * center show, and how is it grouped" is only ever written once,
 * regardless of which surface it ends up embedded in.
 */
(function (global) {
  'use strict';

  // Maps a quality_report "issue" id (cleaning/quality_report.py) to
  // one of the four buckets the control center is organized around.
  // Anything not listed here falls through to "standardization" by
  // default (see categoryFor), rather than being silently dropped -
  // a new issue type added to the backend later still shows up
  // somewhere instead of vanishing from the panel.
  const CATEGORY_BY_ISSUE = {
    duplicate_rows: 'duplicates',
    possible_duplicate_records: 'duplicates',
    missing_values: 'missing',
    high_missingness: 'missing',
    invalid_email_format: 'invalid',
    suspicious_phone_format: 'invalid',
    impossible_age: 'invalid',
    impossible_date: 'invalid',
    mixed_data_types: 'invalid',
    ambiguous_date_format: 'invalid',
    unrecoverable_scientific_notation: 'invalid',
  };

  const CATEGORY_META = {
    duplicates: { label: 'Duplicates', icon: '\u2b22' },
    missing: { label: 'Missing values', icon: '\u25cb' },
    invalid: { label: 'Invalid fields', icon: '\u26a0' },
    standardization: { label: 'Standardization', icon: '\u2699' },
  };
  const CATEGORY_ORDER = ['duplicates', 'missing', 'invalid', 'standardization'];

  function categoryFor(issue, rule) {
    if (CATEGORY_BY_ISSUE[issue]) return CATEGORY_BY_ISSUE[issue];
    if (rule === 'duplicates') return 'duplicates';
    if (rule === 'missing_values') return 'missing';
    return 'standardization';
  }

  function scoreTier(score) {
    if (score >= 85) return 'good';
    if (score >= 60) return 'warn';
    return 'bad';
  }

  // A stable string key for a finding, used to (a) de-duplicate a
  // finding that appears in both recommendations.safe/ambiguous and
  // the raw report.findings list, and (b) let the DOM side attach a
  // matching data-qc-key attribute so "jump to this" can find it, see
  // clean.js's renderReport()/renderRecommendations().
  function findingKey(f) {
    return [f.issue || '', f.column || '', f.detail || ''].join('|');
  }

  /**
   * Pure function: report + recommendations JSON in, a plain
   * { duplicates: [...], missing: [...], invalid: [...],
   *   standardization: [...] } grouping out. No DOM, no globals -
   * safe to reuse from any surface that already has this JSON.
   */
  function groupActions(report, recommendations) {
    const groups = { duplicates: [], missing: [], invalid: [], standardization: [] };
    const seen = new Set();

    (recommendations && recommendations.safe || []).forEach((rec) => {
      (rec.findings || []).forEach((f) => seen.add(findingKey(f)));
      const first = (rec.findings || [])[0] || {};
      groups[categoryFor(first.issue, rec.rule)].push({
        kind: 'safe',
        rule: rec.rule,
        title: rec.reason || rec.rule,
        detail: (rec.findings || [])
          .map((f) => (f.column ? f.column + ': ' : '') + f.detail)
          .join(' \u00b7 '),
        count: rec.issue_count,
        key: 'rule:' + rec.rule,
      });
    });

    (recommendations && recommendations.ambiguous || []).forEach((f) => {
      seen.add(findingKey(f));
      groups[categoryFor(f.issue, null)].push({
        kind: 'review',
        title: f.column || 'Whole file',
        detail: f.detail,
        severity: f.severity,
        key: findingKey(f),
      });
    });

    // Anything in the raw report that neither bucket already covers
    // (no matching rule, not flagged ambiguous either - shouldn't
    // normally happen given how generate_recommendations works, but
    // this keeps the panel honest if that ever changes).
    (report && report.findings || []).forEach((f) => {
      const key = findingKey(f);
      if (seen.has(key)) return;
      groups[categoryFor(f.issue, null)].push({
        kind: 'review',
        title: f.column || 'Whole file',
        detail: f.detail,
        severity: f.severity,
        key: key,
      });
    });

    return groups;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  /**
   * Renders the panel's inner HTML from report + recommendations.
   * `checkedRules` is a Set of rule ids currently checked (so a
   * "safe" action's toggle reflects the real state), supplied by the
   * caller since only the caller knows where those checkboxes live.
   */
  function renderHTML(report, recommendations, checkedRules, beforeScore) {
    const tier = scoreTier(report.score);
    const c = report.counts || {};
    const groups = groupActions(report, recommendations);

    // Explainable score: once a "before" score has been recorded
    // (see mount()'s update()), show it alongside the current score
    // with a delta, rather than just the current number in
    // isolation - this is what lets the same floating badge reflect
    // "here's what changed" both right after upload (before == after,
    // nothing to compare yet) and right after cleaning runs (before
    // != after, the whole point of running it).
    const hasComparison = beforeScore != null && beforeScore !== report.score;
    const delta = hasComparison ? report.score - beforeScore : 0;
    const deltaLabel = delta > 0 ? `+${delta}` : `${delta}`;
    const scoreBadgeHTML = hasComparison
      ? `<div class="qc-score-compare">
           <div class="qc-score-badge small ${scoreTier(beforeScore)}">${beforeScore}</div>
           <span class="qc-score-arrow">&rarr;</span>
           <div class="qc-score-badge ${tier}">${report.score}${report.grade ? '<span>' + esc(report.grade) + '</span>' : ''}</div>
           <span class="qc-score-delta ${delta >= 0 ? 'good' : 'bad'}">${deltaLabel}</span>
         </div>`
      : `<div class="qc-score-badge ${tier}">${report.score}${report.grade ? '<span>' + esc(report.grade) + '</span>' : ''}</div>`;

    const groupsHTML = CATEGORY_ORDER.map((cat) => {
      const items = groups[cat];
      if (!items.length) return '';
      const meta = CATEGORY_META[cat];
      const itemsHTML = items.map((it) => {
        if (it.kind === 'safe') {
          const checked = checkedRules && checkedRules.has(it.rule);
          return `
            <label class="qc-action qc-action-safe">
              <input type="checkbox" class="qc-toggle" data-rule="${esc(it.rule)}" ${checked ? 'checked' : ''}>
              <span class="qc-action-body">
                <strong>${esc(it.title)}</strong>
                <span class="qc-action-detail">${esc(it.detail)}</span>
              </span>
              <span class="qc-count">${it.count != null ? it.count : ''}</span>
            </label>`;
        }
        return `
          <button type="button" class="qc-action qc-action-review" data-jump="${esc(it.key)}">
            <span class="qc-dot qc-dot-${esc(it.severity || 'info')}"></span>
            <span class="qc-action-body">
              <strong>${esc(it.title)}</strong>
              <span class="qc-action-detail">${esc(it.detail)}</span>
            </span>
            <span class="qc-jump-arrow">&rsaquo;</span>
          </button>`;
      }).join('');

      return `
        <div class="qc-group">
          <div class="qc-group-title"><span>${meta.icon}</span>${meta.label}<span class="qc-group-count">${items.length}</span></div>
          <div class="qc-group-items">${itemsHTML}</div>
        </div>`;
    }).join('');

    const totalActions = CATEGORY_ORDER.reduce((n, cat) => n + groups[cat].length, 0);

    return `
      <div class="qc-panel-header">
        ${scoreBadgeHTML}
        <div class="qc-panel-headline">
          <strong>Data quality control center</strong>
          <span class="qc-panel-sub">${report.row_count} rows &middot; ${report.column_count} columns &middot; ${c.critical || 0} critical, ${c.warning || 0} warning, ${c.info || 0} info</span>
        </div>
      </div>
      <div class="qc-panel-body">
        ${totalActions ? groupsHTML : '<p class="qc-empty">No issues found. This file looks clean already.</p>'}
      </div>`;
  }

  /**
   * mount(opts) wires a floating button + panel pair to live data.
   *   opts.fabEl, opts.panelEl   - the two DOM elements (markup only,
   *                                 no assumptions about their ids).
   *   opts.getCheckedRules()     - () => Set<string> of rule ids
   *                                 currently checked in the caller's
   *                                 own rules UI.
   *   opts.onToggleRule(ruleId)  - called when the panel's own
   *                                 checkbox for a safe action is
   *                                 toggled; the caller owns actually
   *                                 flipping its real checkbox (and
   *                                 whatever sync that triggers).
   *   opts.onJump(key)           - called when a "needs review" item
   *                                 is clicked; the caller owns
   *                                 finding + scrolling to + briefly
   *                                 highlighting the matching element
   *                                 (see clean.js's data-qc-key).
   *
   * Returns { update(report, recommendations), show(), hide(), close() }.
   *
   * update() can be called more than once for the same job: the
   * FIRST report it's given (typically the pre-clean GET /api/report
   * result) is remembered as the "before" score, so a later call
   * with the post-clean report (POST /api/process's
   * summary.quality_report - recommendations can be omitted/null for
   * that call, only report.findings is needed) makes the panel show
   * a before -> after comparison instead of just the latest number
   * in isolation, and re-groups its action list from whatever the
   * post-clean report still flags, so already-applied safe fixes
   * stop being listed as pending actions. See renderHTML()'s
   * beforeScore handling.
   */
  function mount(opts) {
    const { fabEl, panelEl } = opts;
    let currentReport = null;
    let currentRecommendations = null;
    let beforeScore = null;

    function isOpen() {
      return !panelEl.classList.contains('hidden');
    }

    function close() {
      panelEl.classList.add('hidden');
      fabEl.setAttribute('aria-expanded', 'false');
    }

    function open() {
      panelEl.classList.remove('hidden');
      fabEl.setAttribute('aria-expanded', 'true');
    }

    fabEl.addEventListener('click', () => {
      if (isOpen()) close();
      else open();
    });

    document.addEventListener('click', (e) => {
      if (!isOpen()) return;
      if (panelEl.contains(e.target) || fabEl.contains(e.target)) return;
      close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && isOpen()) close();
    });

    panelEl.addEventListener('change', (e) => {
      const target = e.target;
      if (target && target.classList.contains('qc-toggle')) {
        opts.onToggleRule && opts.onToggleRule(target.dataset.rule);
      }
    });
    panelEl.addEventListener('click', (e) => {
      const btn = e.target.closest('.qc-action-review');
      if (btn) {
        opts.onJump && opts.onJump(btn.dataset.jump);
        close();
      }
    });

    function update(report, recommendations) {
      currentReport = report;
      currentRecommendations = recommendations;
      if (!report) {
        fabEl.classList.add('hidden');
        close();
        beforeScore = null; // next update() (a new file's report) starts a fresh comparison
        return;
      }
      if (beforeScore === null) beforeScore = report.score;
      const tier = scoreTier(report.score);
      fabEl.classList.remove('hidden');
      fabEl.className = 'qc-fab ' + tier;
      const fabDelta = beforeScore !== report.score ? report.score - beforeScore : 0;
      fabEl.innerHTML = fabDelta
        ? `<span class="qc-fab-score">${report.score}</span><span class="qc-fab-delta ${fabDelta >= 0 ? 'good' : 'bad'}">${fabDelta > 0 ? '+' : ''}${fabDelta}</span>`
        : `<span class="qc-fab-score">${report.score}</span>`;
      panelEl.innerHTML = renderHTML(
        report,
        recommendations,
        opts.getCheckedRules ? opts.getCheckedRules() : new Set(),
        beforeScore
      );
    }

    // Re-render in place (e.g. after the caller's own checkbox
    // changed and the panel should reflect the new checked state)
    // without needing a fresh report/recommendations fetch.
    function refresh() {
      if (currentReport) update(currentReport, currentRecommendations);
    }

    return {
      update, refresh,
      show: () => fabEl.classList.remove('hidden'),
      // hide() is used at every "starting a new file/job" point in
      // clean.js, not just visual hiding - it also drops the
      // remembered before-score so the next update() (that new
      // file's own pre-clean report) starts its own comparison
      // instead of inheriting a stale delta from whatever file was
      // open before it.
      hide: () => { fabEl.classList.add('hidden'); close(); beforeScore = null; },
      close,
    };
  }

  global.OmixaQualityPanel = { mount, groupActions, findingKey };
})(window);
