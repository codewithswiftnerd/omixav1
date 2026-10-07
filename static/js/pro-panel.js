// Workspace additions for signed-in Pro users. Free users see one quiet line, nothing else.
window.OmixaPro = (function () {
  let isPro = false;
  const $ = id => document.getElementById(id);
  (async function init() {
    try {
      const m = await Omixa.me();
      if (!m.user || !m.user.is_pro) return;
      isPro = true;
      const r = await Omixa.api('/api/profiles/');
      if (!r.ok) return;
      const sel = $('profileSelect');
      (r.json.profiles || []).forEach(p => { const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; sel.appendChild(o); });
      $('proPanel').classList.remove('hidden');
      $('proTeaser').classList.add('hidden');
    } catch (e) { /* the free tool never depends on accounts */ }
  })();
  return {
    profileId: () => (isPro && $('profileSelect') ? $('profileSelect').value : ''),
    showResults: function (summary) {
      const el = $('proResults');
      if (!el) return;
      if (isPro && summary.pro) Omixa.renderPro(el, summary.pro, summary.pro.session_id);
      else el.innerHTML = '';
    },
  };
})();
