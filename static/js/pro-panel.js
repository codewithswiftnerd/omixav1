// Workspace additions for signed-in Pro users. Free users see the same options locked, inside "Customize cleaning".
// Locked Pro rows in "Customize cleaning": tapping one explains what to do instead of doing nothing.
document.addEventListener('click', function (e) {
  var item = e.target.closest && e.target.closest('.pro-item[data-pro]');
  if (!item || document.body.classList.contains('is-pro')) return;
  var note = item.parentElement.querySelector('.pro-note');
  if (!note) return;
  var show = note.classList.contains('hidden');
  note.classList.toggle('hidden', !show);
  item.setAttribute('aria-expanded', show ? 'true' : 'false');
});

window.OmixaPro = (function () {
  let isPro = false;
  const $ = id => document.getElementById(id);
  (async function init() {
    try {
      const m = await Omixa.me();
      if (!m.user || !m.user.is_pro) return;
      isPro = true;
      document.body.classList.add('is-pro');
      const r = await Omixa.api('/api/profiles/');
      if (!r.ok) return;
      const sel = $('profileSelect');
      (r.json.profiles || []).forEach(p => { const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; sel.appendChild(o); });
      document.body.classList.add('is-pro');
      $('proPanel').classList.remove('hidden');
    } catch (e) { /* the free tool never depends on accounts */ }
  })();
  return {
    isPro: () => isPro,
    profileId: () => (isPro && $('profileSelect') ? $('profileSelect').value : ''),
    showResults: function (summary) {
      const el = $('proResults');
      if (!el) return;
      if (isPro && summary.pro) Omixa.renderPro(el, summary.pro, summary.pro.session_id);
      else el.innerHTML = '';
    },
  };
})();
