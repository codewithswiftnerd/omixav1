// Feedback form. Validates in the browser for fast feedback; the server validates everything again.
(function () {
  const form = document.getElementById('feedbackForm');
  if (!form) return;
  const $ = id => document.getElementById(id);
  const status = $('fStatus'), btn = $('fSubmit'), msg = $('fMessage'), count = $('fCount');
  const EMAIL = /^[^\s@,;<>"()\[\]\\]+@[^\s@,;<>"()\[\]\\]+\.[^\s@,;<>"()\[\]\\]+$/;

  function show(text, kind) {
    status.textContent = text;
    status.className = 'alert alert-' + kind;
    status.classList.remove('hidden');
  }
  function clearErrors() {
    status.classList.add('hidden');
    form.querySelectorAll('.field-error').forEach(el => el.remove());
    form.querySelectorAll('[aria-invalid]').forEach(el => el.removeAttribute('aria-invalid'));
  }
  function fieldError(el, text) {
    el.setAttribute('aria-invalid', 'true');
    const p = document.createElement('div');
    p.className = 'hint field-error';
    p.style.color = 'var(--danger)';
    p.textContent = text;
    (el.closest('.field') || el.parentNode).appendChild(p);
  }
  const holders = { rating: $('fRatingField'), category: $('fCategory'), message: msg, name: $('fName'), email: $('fEmail') };
  const selectedRating = () => { const r = form.querySelector('input[name="rating"]:checked'); return r ? Number(r.value) : 0; };

  function validate(v) {
    const e = {};
    if (!(v.rating >= 1 && v.rating <= 5)) e.rating = 'Please choose a rating from 1 to 5 stars.';
    if (v.message.length < 10) e.message = 'Please write a little more (at least 10 characters).';
    if (v.email && !EMAIL.test(v.email)) e.email = 'That email address does not look right. You can also leave it empty.';
    return e;
  }

  msg.addEventListener('input', () => { count.textContent = msg.value.length; });

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    if (btn.getAttribute('aria-busy') === 'true') return;
    clearErrors();
    const v = {
      rating: selectedRating(), category: $('fCategory').value, message: msg.value.trim(),
      name: $('fName').value.trim(), email: $('fEmail').value.trim(),
    };
    const errs = validate(v);
    const keys = Object.keys(errs);
    if (keys.length) {
      keys.forEach(k => fieldError(holders[k], errs[k]));
      const first = holders[keys[0]];
      (first.querySelector && first.querySelector('input') || first).focus();
      return;
    }
    btn.setAttribute('aria-busy', 'true'); btn.disabled = true;
    try {
      const r = await Omixa.api('/api/feedback', { method: 'POST', body: Object.assign(v, { website: $('fWebsite').value, token: $('fToken').value }) });
      if (r.ok) {
        form.reset(); count.textContent = '0';
        show(r.json.message || 'Thank you for your feedback.', 'success');
      } else if (r.status === 422 && r.json.fields) {
        Object.keys(r.json.fields).forEach(k => holders[k] && fieldError(holders[k], r.json.fields[k]));
        show(r.json.error || 'Please check the highlighted fields.', 'error');
      } else {
        show(r.json.error || 'Something went wrong. Please try again.', 'error');
      }
    } catch (err) {
      show('We could not reach the server. Check your connection and try again.', 'error');
    } finally {
      btn.removeAttribute('aria-busy'); btn.disabled = false;
    }
  });
})();
