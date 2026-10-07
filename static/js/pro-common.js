// Shared helpers for the account pages. No secrets here: the browser only ever holds a signed
// session cookie, and every Pro decision is made by the server.
window.Omixa = window.Omixa || (function () {
  const base = (window.OMIXA_API_BASE || '').trim().replace(/\/$/, '') || window.location.origin;
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function fmtDate(ts) {
    return ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '–';
  }
  function fmtDay(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { dateStyle: 'medium' }) : '–';
  }
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', credentials: 'include', headers: {} };
    if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const res = await fetch(base + path, init);
    let json = {};
    try { json = await res.json(); } catch (e) { /* non-JSON */ }
    return { ok: res.ok, status: res.status, json };
  }
  function goLogin() {
    window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
  }
  async function me() {
    const r = await api('/api/auth/me');
    return r.ok ? r.json : { user: null, accounts_available: false };
  }
  return { base, esc, fmtDate, fmtDay, api, me, goLogin };
})();
