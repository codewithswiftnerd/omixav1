// Admin dashboard: fetches JSON from /admin/api/* (session-cookie
// authenticated, same-origin) and renders it. No secrets live here —
// the CSRF token used for the purge button comes from a value the
// server already rendered into the page (window.OMIXA_ADMIN_CSRF),
// scoped to this admin's own session.
(function () {
  const $ = (id) => document.getElementById(id);
  const csrf = window.OMIXA_ADMIN_CSRF || '';

  function fmtPct(v) { return v === null || v === undefined ? '\u2013' : v + '%'; }
  function fmtMs(v) {
    if (v === null || v === undefined) return '\u2013';
    return v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms';
  }
  function fmtDate(ts) {
    if (!ts) return '\u2013';
    return new Date(ts * 1000).toLocaleString();
  }
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function loadUsers() {
    try {
      const res = await fetch('/admin/api/users', { credentials: 'same-origin' });
      if (!res.ok) return;
      const u = await res.json();
      if (!u.available) { ['statUsers','statProUsers','statNewUsers7','statNewUsers30'].forEach(id => { $(id).textContent = 'n/a'; }); return; }
      $('statUsers').textContent = u.total_users;
      $('statProUsers').textContent = u.pro_users;
      $('statNewUsers7').textContent = u.new_last_7d;
      $('statNewUsers30').textContent = u.new_last_30d;
    } catch (e) { /* leave placeholders */ }
  }

  async function loadStats() {
    const windowVal = $('windowSelect').value;
    const res = await fetch('/admin/api/stats?window=' + encodeURIComponent(windowVal), { credentials: 'same-origin' });
    if (!res.ok) return;
    const s = await res.json();

    $('statTotalJobs').textContent = s.total_jobs ?? '\u2013';
    $('statSessions').textContent = s.distinct_sessions ?? '\u2013';
    $('statSuccessRate').textContent = fmtPct(s.processing_success_rate_pct);
    $('statAvgTime').textContent = fmtMs(s.avg_processing_ms);
    $('statQualityBefore').textContent = s.avg_quality_score_before ?? '\u2013';
    $('statQualityAfter').textContent = s.avg_quality_score_after ?? '\u2013';
    $('statFailures').textContent = s.processing_failure_count ?? '\u2013';
    $('statErrors').textContent = s.error_count ?? '\u2013';

    const formats = s.by_format || {};
    $('formatBreakdown').innerHTML = Object.keys(formats).length
      ? Object.entries(formats).map(([k, v]) => `<span class="badge badge-info" style="margin:0 6px 6px 0;">${esc(k.toUpperCase())}: ${v}</span>`).join('')
      : '<p style="color:var(--text-secondary);">No uploads yet.</p>';

    const rules = s.top_rules || [];
    $('ruleBreakdown').innerHTML = rules.length
      ? '<ol>' + rules.map(([name, n]) => `<li>${esc(name)} &mdash; ${n} job(s)</li>`).join('') + '</ol>'
      : '<p style="color:var(--text-secondary);">No processed jobs yet.</p>';

    const issues = s.top_issues || [];
    $('issueBreakdown').innerHTML = issues.length
      ? '<ol>' + issues.map(([name, n]) => `<li>${esc(name)} &mdash; ${n} job(s)</li>`).join('') + '</ol>'
      : '<p style="color:var(--text-secondary);">No issues detected yet.</p>';
  }

  function statusBadge(status) {
    const map = { processed: 'success', downloaded: 'success', uploaded: 'info', failed: 'danger', expired: 'warning' };
    const cls = map[status] || 'info';
    return `<span class="badge badge-${cls}">${esc(status || 'unknown')}</span>`;
  }

  async function loadJobs() {
    const params = new URLSearchParams();
    const q = $('jobSearch').value.trim();
    const status = $('statusFilter').value;
    const format = $('formatFilter').value;
    if (q) params.set('q', q);
    if (status) params.set('status', status);
    if (format) params.set('format', format);
    params.set('limit', '50');

    const res = await fetch('/admin/api/jobs?' + params.toString(), { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    const tbody = $('jobsTableBody');
    const jobs = data.jobs || [];

    $('jobsEmpty').style.display = jobs.length ? 'none' : '';
    tbody.innerHTML = jobs.map((j) => {
      const quality = (j.quality_score_before ?? '\u2013') + ' \u2192 ' + (j.quality_score_after ?? '\u2013');
      return `<tr>
        <td><code style="font-size:0.78rem;">${esc((j.job_id || '').slice(0, 8))}&hellip;</code></td>
        <td>${esc(j.original_filename || '\u2013')}</td>
        <td>${esc((j.original_ext || '').toUpperCase())}</td>
        <td>${statusBadge(j.status)}</td>
        <td>${j.rows_in ?? '\u2013'} &rarr; ${j.rows_out ?? '\u2013'}</td>
        <td>${quality}</td>
        <td>${fmtDate(j.created_at)}</td>
      </tr>`;
    }).join('');
  }

  async function loadErrors() {
    const res = await fetch('/admin/api/errors', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    const errors = data.errors || [];
    $('errorsList').innerHTML = errors.length
      ? '<table><thead><tr><th>When</th><th>Route</th><th>Type</th><th>Job</th></tr></thead><tbody>' +
        errors.map((e) => `<tr><td>${fmtDate(e.ts)}</td><td>${esc(e.route)}</td><td>${esc(e.error_type)}</td><td><code style="font-size:0.78rem;">${esc((e.job_id || '').slice(0, 8))}</code></td></tr>`).join('') +
        '</tbody></table>'
      : '<p style="color:var(--text-secondary);">No errors logged.</p>';
  }

  async function loadLogins() {
    const res = await fetch('/admin/api/logins', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    const attempts = data.attempts || [];
    $('loginsList').innerHTML = attempts.length
      ? '<table><thead><tr><th>When</th><th>Result</th></tr></thead><tbody>' +
        attempts.map((a) => `<tr><td>${fmtDate(a.ts)}</td><td>${a.success ? '<span class="badge badge-success">success</span>' : '<span class="badge badge-danger">failed</span>'}</td></tr>`).join('') +
        '</tbody></table>'
      : '<p style="color:var(--text-secondary);">No login attempts logged.</p>';
  }

  async function refreshAll() {
    await Promise.all([loadStats(), loadUsers(), loadJobs(), loadErrors(), loadLogins()]);
  }

  $('windowSelect').addEventListener('change', loadStats);
  $('jobSearch').addEventListener('input', debounce(loadJobs, 300));
  $('statusFilter').addEventListener('change', loadJobs);
  $('formatFilter').addEventListener('change', loadJobs);

  $('purgeBtn').addEventListener('click', async () => {
    $('purgeBtn').disabled = true;
    try {
      await fetch('/admin/api/jobs/purge', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRF-Token': csrf },
      });
      await refreshAll();
    } finally {
      $('purgeBtn').disabled = false;
    }
  });

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  refreshAll();
})();
