// Renders the Pro analysis block (same markup on the results screen and the saved-session page).
(function () {
  const { esc } = Omixa;
  const mark = { PASS: '✓', WARNING: '⚠', FAIL: '✕' };
  const cls = s => s >= 85 ? 'badge-success' : s >= 60 ? 'badge-warning' : 'badge-danger';
  const stCls = s => ({ PASS: 'badge-success', WARNING: 'badge-warning', FAIL: 'badge-danger' }[s]);
  const n = v => (v == null ? '–' : Number(v).toLocaleString());

  function delta(b, a, lowerBetter) {
    if (b == null || a == null || b === a) return '';
    const good = lowerBetter ? a < b : a > b;
    return ` <small style="color:var(--${good ? 'success' : 'danger'})">(${a > b ? '+' : ''}${a - b})</small>`;
  }

  Omixa.renderPro = function (el, pro, sessionId) {
    const h = pro.headline, c = pro.comparison, w = pro.what_changed, p = pro.profile;
    const dims = Object.values(pro.dimensions || {}).filter(d => d.applicable !== false);
    let html = `
      <div class="card">
        <div class="card-title-row"><h3>Data quality</h3><span class="badge ${cls(h.score)}">${esc(h.score)}/100 · ${esc(h.grade)}</span></div>
        <p><strong>${n(h.records)}</strong> records · <strong>${esc(h.columns)}</strong> columns</p>
        <ul class="price-list">
          <li>${n(h.duplicate_records)} duplicate records</li>
          <li>${n(h.missing_values)} missing values</li>
          <li>${n(h.invalid_dates)} invalid dates</li>
          <li>${n(h.inconsistent_category_values)} inconsistent category values</li>
          <li>${n(h.critical_structural_errors)} critical structural errors</li>
        </ul>
        <p class="hint">Changes made: <strong>${n(w.changes_made)}</strong> · Needing review: <strong>${n(w.needs_review)}</strong></p>
      </div>`;

    if (p) {
      html += `<div class="card"><div class="card-title-row"><h3>${esc(p.name)}</h3><span class="badge ${cls(p.compliance_after)}">Compliance ${esc(p.compliance_after)}%</span></div>
        ${p.compliance_before != null ? `<p class="hint">Before cleaning: ${esc(p.compliance_before)}%</p>` : ''}
        <table><tbody>${p.results.map(r => `<tr><td style="width:2.2rem"><span class="badge ${stCls(r.status)}">${mark[r.status]}</span></td>
          <td><strong>${esc(r.label)}</strong><div class="hint">${esc(r.detail)}</div></td>
          <td class="hint" style="text-align:right">${esc(r.status)}${r.before_status && r.before_status !== r.status ? '<br>was ' + esc(r.before_status) : ''}</td></tr>`).join('')}</tbody></table></div>`;
    }

    html += `<div class="card"><h3>What changed</h3><ul class="price-list">${w.lines.map(l => `<li>${l.status === 'ok' ? '✓' : '⚠'} ${esc(l.text)}</li>`).join('')}</ul></div>`;

    const row = (label, k, lower) => `<tr><td>${label}</td><td>${n(c.before[k])}</td><td>${n(c.after[k])}${delta(c.before[k], c.after[k], lower)}</td></tr>`;
    html += `<div class="card"><h3>Before vs after</h3><table><thead><tr><th></th><th>Before</th><th>After</th></tr></thead><tbody>
      ${row('Quality score', 'score', false)}${row('Rows', 'rows', false)}${row('Columns', 'columns', false)}${row('Issues', 'issues', true)}
      ${row('Duplicates', 'duplicates', true)}${row('Missing values', 'missing_values', true)}${row('Validation failures', 'validation_failures', true)}</tbody></table></div>`;

    html += `<div class="card"><h3>Quality breakdown</h3><table><tbody>${dims.map(d => `<tr><td>${esc(d.label)}</td><td><span class="badge ${d.score == null ? '' : cls(d.score)}">${d.score == null ? 'n/a' : esc(d.score) + '/100'}</span></td><td class="hint">${esc(d.issues)} issues</td></tr>`).join('')}</tbody></table>
      <p class="hint">${esc(pro.severity.critical)} critical · ${esc(pro.severity.warning)} warning · ${esc(pro.severity.information)} information</p></div>`;

    html += `<div class="card"><h3>Column quality</h3><table><thead><tr><th>Column</th><th>Score</th><th>Issues</th></tr></thead><tbody>${(pro.columns || []).slice(0, 40).map(col =>
      `<tr><td>${esc(col.column)}</td><td><span class="badge ${cls(col.score)}">${esc(col.score)}</span></td><td class="hint">${col.issues.length ? col.issues.slice(0, 3).map(i => esc(i.title) + ' (' + esc(i.count) + ')').join('; ') : 'None'}</td></tr>`).join('')}</tbody></table></div>`;

    if ((pro.review_items || []).length) {
      html += `<div class="card"><h3>Needs your review</h3><table><tbody>${pro.review_items.map(i => `<tr><td><span class="badge ${i.severity === 'critical' ? 'badge-danger' : 'badge-warning'}">${esc(i.severity)}</span></td><td>${esc(i.title)}${i.column ? ' · ' + esc(i.column) : ''}<div class="hint">${esc(i.recommended_action || '')}</div></td><td>${n(i.count)}</td></tr>`).join('')}</tbody></table></div>`;
    }

    if (sessionId) {
      const b = Omixa.base + '/api/sessions/' + encodeURIComponent(sessionId);
      html += `<div class="card"><h3>Export</h3><div class="main-actions">
        <a class="btn btn-primary btn-sm" href="${b}/report.pdf">Quality report (PDF)</a>
        <a class="btn btn-secondary btn-sm" href="${b}/what-changed.txt">What changed</a>
        <a class="btn btn-secondary btn-sm" href="${b}/change-log.csv">Change log (CSV)</a></div>
        <p class="hint">Reports contain figures only, never your data. Download the cleaned dataset from the results screen.</p></div>`;
    }
    el.innerHTML = html;
  };
})();
