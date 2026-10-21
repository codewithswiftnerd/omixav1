// Omixa motion behaviour. Loaded on every page after pro-common.js; styles live in css/motion.css.
//
// Rules this file follows:
//  - Nothing here touches user data, uploads or API calls. It only changes how existing content arrives.
//  - Content never depends on JS to be visible: reveal hiding is switched on by a class that is only
//    set when IntersectionObserver exists, is skipped under prefers-reduced-motion (CSS), and is
//    removed again if the observer never reports in.
//  - Count-up only re-displays a number that the app already rendered; it always ends on that exact text.
//  - Everything that animates out waits for a short timer, so it can never leave a control stuck.
(function () {
  'use strict';

  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function reduced() { return !!(mq && mq.matches); }

  // ---------------------------------------------------------------- scroll reveal
  // Mark things with data-reveal (single element) or data-stagger (its children reveal one after another).
  function initReveal() {
    var staggerParents = document.querySelectorAll('[data-stagger]');
    Array.prototype.forEach.call(staggerParents, function (parent) {
      Array.prototype.forEach.call(parent.children, function (child, i) {
        if (!child.hasAttribute('data-reveal')) child.setAttribute('data-reveal', '');
        child.style.setProperty('--d', Math.min(i, 7) * 70 + 'ms');
      });
    });
    var targets = document.querySelectorAll('[data-reveal]');
    if (!targets.length) return;
    if (!('IntersectionObserver' in window)) { root.classList.remove('motion'); return; }

    var reported = false;
    function settle(el) {
      // After the transition, drop the attribute entirely so the element's own hover/transition rules apply again.
      setTimeout(function () {
        el.removeAttribute('data-reveal');
        el.classList.remove('in');
        el.style.removeProperty('--d');
      }, 1100);
    }
    var io = new IntersectionObserver(function (entries) {
      reported = true;
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        io.unobserve(entry.target);
        entry.target.classList.add('in');
        settle(entry.target);
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -6% 0px' });
    Array.prototype.forEach.call(targets, function (el) { io.observe(el); });

    // If the observer never reports (very old/broken WebView), show everything rather than hide it.
    setTimeout(function () { if (!reported) root.classList.remove('motion'); }, 3000);
  }

  // ---------------------------------------------------------------- count-up
  // Statistics the app has already rendered count up once when they appear. Only whole numbers;
  // 0 and 1 are left alone; the element always ends on exactly the text the app wrote.
  var COUNT_WATCH = ['usage', 'stats'];            // containers whose children are (re)rendered by the app
  var COUNT_SEL = '.stat-value, .stat .n';
  function fmt(n, withCommas) { return withCommas ? n.toLocaleString('en-US') : String(n); }
  function countUp(el) {
    if (reduced() || document.hidden || el.getAttribute('data-counted')) return;
    var text = (el.textContent || '').trim();
    if (!/^(\d{1,3}(,\d{3})+|\d+)$/.test(text)) return;
    var target = parseInt(text.replace(/,/g, ''), 10);
    if (!(target > 1) || target > 1e9) return;
    el.setAttribute('data-counted', '1');
    var commas = text.indexOf(',') !== -1;
    var dur = Math.min(900, 450 + String(target).length * 60);
    var start = null;
    el.textContent = fmt(0, commas);
    function tick(now) {
      if (start === null) start = now;
      var p = Math.min((now - start) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = p < 1 ? fmt(Math.round(target * eased), commas) : text;
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    setTimeout(function () { el.textContent = text; }, dur + 250);   // guarantees the true value even if rAF is throttled
  }
  function scan(node) {
    if (node.nodeType !== 1) return;
    if (node.matches && node.matches(COUNT_SEL)) countUp(node);
    if (node.querySelectorAll) Array.prototype.forEach.call(node.querySelectorAll(COUNT_SEL), countUp);
  }
  function initCountUp() {
    if (!('MutationObserver' in window)) return;
    COUNT_WATCH.forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      new MutationObserver(function (muts) {
        muts.forEach(function (m) { Array.prototype.forEach.call(m.addedNodes, scan); });
      }).observe(el, { childList: true, subtree: true });
    });
  }

  // ---------------------------------------------------------------- navigation state
  function initNav() {
    var nav = document.querySelector('.topnav');
    if (nav) {
      var ticking = false;
      function update() { nav.classList.toggle('is-scrolled', window.scrollY > 4); ticking = false; }
      window.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
      update();
    }

    var links = document.querySelectorAll('#navLinks a:not(.nav-cta)');
    if (!links.length) return;
    function clear() { Array.prototype.forEach.call(links, function (a) { a.removeAttribute('aria-current'); }); }
    var path = window.location.pathname.replace(/\/$/, '') || '/';

    // Page links (Pricing, Dashboard, ...): mark the one we are on.
    Array.prototype.forEach.call(links, function (a) {
      var u; try { u = new URL(a.href, window.location.origin); } catch (e) { return; }
      var p = u.pathname.replace(/\/$/, '') || '/';
      if (!u.hash && p !== '/' && p === path) a.setAttribute('aria-current', 'page');
    });

    // Landing page: follow the section in view for the #anchor links.
    if (path === '/' && 'IntersectionObserver' in window) {
      var map = {};
      Array.prototype.forEach.call(links, function (a) { if (a.hash) map[a.hash.slice(1)] = a; });
      var sections = Object.keys(map).map(function (id) { return document.getElementById(id); }).filter(Boolean);
      if (!sections.length) return;
      var spy = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (!e.isIntersecting) return;
          Array.prototype.forEach.call(links, function (a) { if (a.hash) a.removeAttribute('aria-current'); });
          if (map[e.target.id]) map[e.target.id].setAttribute('aria-current', 'location');
        });
      }, { rootMargin: '-40% 0px -55% 0px' });
      sections.forEach(function (s) { spy.observe(s); });
      window.addEventListener('scroll', function () {
        if (window.scrollY < 200) Array.prototype.forEach.call(links, function (a) { if (a.hash) a.removeAttribute('aria-current'); });
      }, { passive: true });
    }
  }

  // ---------------------------------------------------------------- toast
  var region = null;
  function toastRegion() {
    if (region && document.body.contains(region)) return region;
    region = document.createElement('div');
    region.className = 'toast-region';
    region.setAttribute('role', 'region');
    region.setAttribute('aria-label', 'Notifications');
    document.body.appendChild(region);
    return region;
  }
  function dismiss(t) {
    if (!t || t.getAttribute('data-leaving')) return;
    t.setAttribute('data-leaving', '1');
    t.classList.add('leaving');
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, reduced() ? 0 : 240);
  }
  // Omixa.toast('Saved', { type: 'success' | 'error' | 'info', duration: ms }). Errors stay until dismissed.
  function toast(message, opts) {
    opts = opts || {};
    var type = opts.type || 'info';
    var t = document.createElement('div');
    t.className = 'toast toast-' + type;
    t.setAttribute('role', type === 'error' ? 'alert' : 'status');
    var msg = document.createElement('div');
    msg.className = 'toast-msg';
    msg.textContent = message;                       // textContent: never interpret the message as HTML
    var close = document.createElement('button');
    close.type = 'button'; close.className = 'toast-close'; close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '\u00d7';
    close.addEventListener('click', function () { dismiss(t); });
    t.appendChild(msg); t.appendChild(close);
    toastRegion().appendChild(t);
    var duration = opts.duration != null ? opts.duration : (type === 'error' ? 0 : 4500);
    if (duration > 0) setTimeout(function () { dismiss(t); }, duration);
    return t;
  }

  // ---------------------------------------------------------------- confirm dialog
  // Omixa.confirm('Delete this profile?', { title, confirmLabel, cancelLabel, danger }) -> Promise<boolean>
  // Built on <dialog>.showModal(), so focus is trapped, Escape cancels, and the page behind is inert.
  // Falls back to window.confirm() where <dialog> is unavailable.
  function confirmDialog(message, opts) {
    opts = opts || {};
    if (typeof HTMLDialogElement === 'undefined' || !document.createElement('dialog').showModal) {
      return Promise.resolve(window.confirm(message));
    }
    return new Promise(function (resolve) {
      var d = document.createElement('dialog');
      d.className = 'm-dialog';
      d.setAttribute('aria-labelledby', 'mDialogTitle');
      d.setAttribute('aria-describedby', 'mDialogMsg');
      var body = document.createElement('div'); body.className = 'm-dialog-body';
      var h = document.createElement('h2'); h.id = 'mDialogTitle'; h.textContent = opts.title || 'Are you sure?';
      var p = document.createElement('p'); p.id = 'mDialogMsg'; p.textContent = message;
      body.appendChild(h); body.appendChild(p);
      var actions = document.createElement('div'); actions.className = 'm-dialog-actions';
      var no = document.createElement('button'); no.type = 'button'; no.className = 'btn btn-secondary'; no.textContent = opts.cancelLabel || 'Cancel';
      var yes = document.createElement('button'); yes.type = 'button';
      yes.className = 'btn ' + (opts.danger === false ? 'btn-primary' : 'btn-danger-solid'); yes.textContent = opts.confirmLabel || 'Confirm';
      actions.appendChild(no); actions.appendChild(yes);
      d.appendChild(body); d.appendChild(actions);
      document.body.appendChild(d);

      var result = false, finished = false;
      function finish() {
        if (finished) return; finished = true;
        if (d.open) d.close();
        if (d.parentNode) d.parentNode.removeChild(d);
        resolve(result);
      }
      function closeWith(value) {
        result = value;
        if (reduced()) { finish(); return; }
        d.classList.add('closing');
        setTimeout(finish, 190);
      }
      yes.addEventListener('click', function () { closeWith(true); });
      no.addEventListener('click', function () { closeWith(false); });
      d.addEventListener('cancel', function (e) { e.preventDefault(); closeWith(false); });   // Escape
      d.addEventListener('click', function (e) { if (e.target === d) closeWith(false); });    // click on the backdrop
      d.showModal();
      no.focus();                                      // safest default for a destructive prompt
    });
  }

  // ---------------------------------------------------------------- public surface
  window.Omixa = window.Omixa || {};
  window.Omixa.toast = toast;
  window.Omixa.confirm = confirmDialog;
  window.Omixa.motion = { reduced: reduced };

  function init() { initReveal(); initCountUp(); initNav(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
