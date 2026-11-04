// Omixa interactive layer. Loaded after motion.js on every page; styles live in css/interactive.css.
//
// Rules this file follows (same spirit as motion.js):
//  - It never touches user data, uploads or API calls. The one exception is the page-wide drop target,
//    which hands a dropped file to the existing #fileInput 'change' handler, exactly like choosing it by hand.
//  - Nothing here is needed to read or use the page. Every module checks for what it needs and does nothing if absent.
//  - Reduced motion: no aurora, tilt, magnet, ripple, scramble, confetti or looping demo. The demo shows its final state.
//  - Hover-driven effects only run on fine pointers (mouse/trackpad), never on touch.
//  - Demo values below are labelled example data on the page and mirror real Omixa behaviour (see home.js).
(function () {
  'use strict';

  var root = document.documentElement;
  var mqReduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var mqFine = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : null;
  function reduced() { return !!(mqReduce && mqReduce.matches); }
  function fine() { return !!(mqFine && mqFine.matches); }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---------------------------------------------------------------- scramble text
  // Reveals finalText left to right while the rest shuffles. Always ends on exactly finalText.
  var GLYPHS = 'abcdefghijklmnopqrstuvwxyz0123456789';
  var KEEP = /[\s\-\/@.()+:,]/;
  function scramble(setText, finalText, dur, delay) {
    if (reduced() || !finalText) { setText(finalText); return; }
    var len = finalText.length, t0 = null, done = false;
    function finish() { if (!done) { done = true; setText(finalText); } }
    function frame(now) {
      if (done) return;
      if (t0 === null) t0 = now;
      var p = clamp((now - t0) / dur, 0, 1);
      if (p >= 1) { finish(); return; }
      var k = Math.floor(p * len), out = '';
      for (var i = 0; i < len; i++) {
        var ch = finalText.charAt(i);
        out += (i < k || KEEP.test(ch)) ? ch : GLYPHS.charAt((Math.random() * GLYPHS.length) | 0);
      }
      setText(out);
      requestAnimationFrame(frame);
    }
    setTimeout(function () { requestAnimationFrame(frame); }, delay || 0);
    setTimeout(finish, (delay || 0) + dur + 160);   // guarantees the true value even if rAF is throttled
  }

  // ---------------------------------------------------------------- aurora + scroll progress
  function initAmbient() {
    var bar = el('div', 'scroll-progress');
    bar.setAttribute('aria-hidden', 'true');
    document.body.appendChild(bar);
    var tickingBar = false;
    function updateBar() {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      bar.style.transform = 'scaleX(' + (max > 0 ? clamp(window.scrollY / max, 0, 1) : 0) + ')';
      tickingBar = false;
    }
    window.addEventListener('scroll', function () { if (!tickingBar) { tickingBar = true; requestAnimationFrame(updateBar); } }, { passive: true });
    window.addEventListener('resize', updateBar);
    updateBar();

    if (reduced()) return;
    var wrap = el('div', 'aurora');
    wrap.setAttribute('aria-hidden', 'true');
    var blobs = [el('i'), el('i'), el('i')];
    blobs.forEach(function (b) { wrap.appendChild(b); });
    document.body.insertBefore(wrap, document.body.firstChild);

    var W = window.innerWidth, H = window.innerHeight;
    // base position (fraction of viewport), how far each blob follows the cursor, and scroll parallax
    var cfg = [
      { bx: 0.82, by: 0.14, follow: 0.10, par: 0.10 },
      { bx: 0.08, by: 0.42, follow: -0.06, par: 0.06 },
      { bx: 0.88, by: 0.82, follow: 0.04, par: 0.14 }
    ];
    var cur = cfg.map(function (c) { return { x: c.bx * W, y: c.by * H }; });
    var mx = W / 2, my = H / 2, sy = window.scrollY, raf = 0;

    function frame() {
      var moving = false;
      for (var i = 0; i < cfg.length; i++) {
        var c = cfg[i];
        var tx = c.bx * W + (mx - W / 2) * c.follow * 2;
        var ty = c.by * H + (my - H / 2) * c.follow * 2 - sy * c.par;
        cur[i].x += (tx - cur[i].x) * 0.06;
        cur[i].y += (ty - cur[i].y) * 0.06;
        if (Math.abs(tx - cur[i].x) > 0.15 || Math.abs(ty - cur[i].y) > 0.15) moving = true;
        blobs[i].style.transform = 'translate3d(' + cur[i].x.toFixed(1) + 'px,' + cur[i].y.toFixed(1) + 'px,0)';
      }
      raf = moving ? requestAnimationFrame(frame) : 0;
    }
    function kick() { if (!raf) raf = requestAnimationFrame(frame); }
    if (fine()) window.addEventListener('pointermove', function (e) { mx = e.clientX; my = e.clientY; kick(); }, { passive: true });
    window.addEventListener('scroll', function () { sy = window.scrollY; kick(); }, { passive: true });
    window.addEventListener('resize', function () { W = window.innerWidth; H = window.innerHeight; kick(); });
    kick();
  }

  // ---------------------------------------------------------------- card spotlight + tilt + magnetic buttons
  // Hosts are found from the event target, so cards that the app renders later (reports, history) work too.
  var SPOT_SEL = '.tool-card, .trust-item, .price-card, .live-demo, .stat-card, .card, .auth-card';
  var TILT = { '.tool-card': { ax: 3.2, ay: 4.4, lift: 0 }, '.price-card': { ax: 2.2, ay: 3, lift: -3 }, '.stat-card': { ax: 1.8, ay: 2.4, lift: -2 } };
  var TILT_SEL = Object.keys(TILT).join(', ');
  var MAGNET_SEL = '.btn-upload, .btn-main';
  function initPointerEffects() {
    if (!fine()) return;
    var last = null, pending = false, tiltEl = null;
    function resetTilt() {
      if (!tiltEl) return;
      tiltEl.classList.remove('is-tilting');
      tiltEl.style.transform = '';
      tiltEl = null;
    }
    function magnets() { return Array.prototype.slice.call(document.querySelectorAll(MAGNET_SEL)); }
    function cfgFor(node) {
      for (var k in TILT) if (node.matches(k)) return TILT[k];
      return null;
    }

    function process() {
      pending = false;
      var e = last;
      if (!e || !e.target || !e.target.closest) return;

      var host = e.target.closest(SPOT_SEL);
      if (host) {
        if (!host.hasAttribute('data-spot')) host.setAttribute('data-spot', '');
        var r = host.getBoundingClientRect();
        host.style.setProperty('--px', (e.clientX - r.left) + 'px');
        host.style.setProperty('--py', (e.clientY - r.top) + 'px');
      }

      // tilt (never on the upload card while a file is being worked on: it holds real controls)
      if (!reduced()) {
        var th = e.target.closest(TILT_SEL);
        if (th && th.matches('.tool-card') && document.body.classList.contains('has-file')) th = null;
        if (th !== tiltEl) resetTilt();
        if (th) {
          var c = cfgFor(th), tr = th.getBoundingClientRect();
          var px = (e.clientX - tr.left) / tr.width - 0.5, py = (e.clientY - tr.top) / tr.height - 0.5;
          th.classList.add('is-tilting');
          th.style.transform = 'perspective(1100px) translateY(' + c.lift + 'px) rotateX(' + (-py * c.ax).toFixed(2) + 'deg) rotateY(' + (px * c.ay).toFixed(2) + 'deg)';
          tiltEl = th;
        }
      }

      // magnetic pull: buttons lean toward the cursor when it is near, and let go when it leaves
      if (!reduced()) {
        magnets().forEach(function (b) {
          if (b.disabled) return;
          var br = b.getBoundingClientRect();
          var cx = br.left + br.width / 2, cy = br.top + br.height / 2;
          var dx = e.clientX - cx, dy = e.clientY - cy;
          var near = Math.abs(dx) < br.width / 2 + 36 && Math.abs(dy) < br.height / 2 + 36;
          if (near) {
            b.classList.add('is-pulled');
            b.style.setProperty('--tx', clamp(dx * 0.14, -9, 9).toFixed(1) + 'px');
            b.style.setProperty('--ty', clamp(dy * 0.2, -7, 7).toFixed(1) + 'px');
          } else if (b.classList.contains('is-pulled')) {
            b.classList.remove('is-pulled');
            b.style.removeProperty('--tx');
            b.style.removeProperty('--ty');
          }
        });
      }
    }
    document.addEventListener('pointermove', function (e) {
      if (e.pointerType && e.pointerType !== 'mouse' && e.pointerType !== 'pen') return;
      last = e;
      if (!pending) { pending = true; requestAnimationFrame(process); }
    }, { passive: true });
    document.addEventListener('mouseleave', function () {
      resetTilt();
      magnets().forEach(function (b) { b.classList.remove('is-pulled'); b.style.removeProperty('--tx'); b.style.removeProperty('--ty'); });
    });
  }

  // ---------------------------------------------------------------- press ripple
  function initRipple() {
    if (reduced()) return;
    document.addEventListener('pointerdown', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('.btn-upload, .btn-main, .btn, .tab') : null;
      if (!b || b.disabled) return;
      var r = b.getBoundingClientRect();
      var size = Math.max(r.width, r.height) * 2;
      var s = el('span', 'ripple');
      s.style.width = s.style.height = size + 'px';
      s.style.left = (e.clientX - r.left - size / 2) + 'px';
      s.style.top = (e.clientY - r.top - size / 2) + 'px';
      b.appendChild(s);
      setTimeout(function () { if (s.parentNode) s.parentNode.removeChild(s); }, 700);
    });
  }

  // ---------------------------------------------------------------- hero headline: word index for the staggered entrance
  function initHeadline() {
    var words = document.querySelectorAll('.hero-copy h1 .w');
    Array.prototype.forEach.call(words, function (w, i) { w.style.setProperty('--wi', i); });
  }

  // ---------------------------------------------------------------- live demo: a messy sheet cleans itself
  function initDemo() {
    var host = document.getElementById('liveDemo');
    if (!host) return;
    var COLS = ['Name', 'Email', 'Phone', 'Joined'];
    // [messy, clean]. Every transformation here is something Omixa really does (see home.js and the cleaning rules).
    var ROWS = [
      [['ADA OBI', 'Ada Obi'], [' ADA@Mail.COM ', 'ada@mail.com'], ['(0803) 123-4567', '08031234567'], ['March 14, 2024', '2024-03-14']],
      [['ADA OBI', 'Ada Obi'], ['ada@mail.com ', 'ada@mail.com'], ['0803 123 4567', '08031234567'], ['2024/3/14', '2024-03-14']],
      [['KOFI MENSAH', 'Kofi Mensah'], ['kofi@mail.com ', 'kofi@mail.com'], ['0722-555-019', '0722555019'], ['2024/3/9', '2024-03-09']]
    ];
    var DUP_INDEX = 1;   // row 2 becomes identical to row 1 once cleaned, so it is removed as a duplicate

    host.setAttribute('role', 'presentation');
    host.setAttribute('aria-hidden', 'true');
    host.title = 'Click to replay';

    var bar = el('div', 'ld-bar');
    bar.appendChild(el('i')); bar.appendChild(el('i')); bar.appendChild(el('i'));
    bar.appendChild(el('span', 'ld-title', 'customers.csv'));
    var badge = el('span', 'ld-badge is-messy', 'Messy');
    bar.appendChild(badge);

    var head = el('div', 'ld-row ld-head');
    COLS.forEach(function (c) { head.appendChild(el('span', '', c)); });

    var body = el('div', 'ld-body');
    var rowEls = [], cells = [];
    var fixes = 0;
    ROWS.forEach(function (row, ri) {
      var r = el('div', 'ld-row r');
      row.forEach(function (pair) {
        var c = el('span', 'ld-cell');
        c.setAttribute('data-messy', pair[0]);
        c.setAttribute('data-clean', pair[1]);
        if (pair[0] !== pair[1]) fixes++;
        r.appendChild(c); cells.push(c);
      });
      body.appendChild(r); rowEls.push(r);
    });
    var beam = el('div', 'ld-beam');
    body.appendChild(beam);

    var foot = el('div', 'ld-foot');
    var sum = el('span', 'ld-sum');
    var note = el('span', '', 'Example data');
    foot.appendChild(sum); foot.appendChild(note);

    host.appendChild(bar); host.appendChild(head); host.appendChild(body); host.appendChild(foot);

    var timers = [], anim = null, loop = true, visible = false, started = false;
    function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
    function stop() {
      timers.forEach(clearTimeout); timers = [];
      if (anim) { anim.cancel(); anim = null; }
    }
    function setBadge(kind, text) { badge.className = 'ld-badge is-' + kind; badge.textContent = text; }
    function summary() {
      return fixes + ' cells fixed \u00b7 1 duplicate removed';
    }
    function showMessy() {
      stop();
      cells.forEach(function (c) {
        c.textContent = c.getAttribute('data-messy');
        c.className = 'ld-cell' + (c.getAttribute('data-messy') !== c.getAttribute('data-clean') ? ' bad' : '');
      });
      rowEls.forEach(function (r) { r.classList.remove('gone', 'dup'); });
      beam.style.opacity = '0';
      setBadge('messy', 'Messy');
      sum.textContent = 'Click to watch it clean';
    }
    function showClean() {
      stop();
      cells.forEach(function (c) { c.textContent = c.getAttribute('data-clean'); c.className = 'ld-cell'; });
      rowEls.forEach(function (r, i) { r.classList.remove('dup'); r.classList.toggle('gone', i === DUP_INDEX); });
      beam.style.opacity = '0';
      setBadge('clean', 'Clean');
      sum.textContent = summary();
    }
    function fixRow(r) {
      Array.prototype.forEach.call(r.children, function (c, j) {
        var clean = c.getAttribute('data-clean');
        scramble(function (t) { c.textContent = t; }, clean, 380, j * 70);
        later(function () { c.className = 'ld-cell fixed'; }, j * 70 + 60);
        later(function () { c.className = 'ld-cell'; }, j * 70 + 1500);
      });
    }
    function play() {
      if (reduced()) { showClean(); return; }
      showMessy();
      setBadge('scan', 'Scanning');
      sum.textContent = 'Checking every cell\u2026';
      var h = body.offsetHeight, dur = 2300;
      if (beam.animate) {
        anim = beam.animate([
          { transform: 'translateY(-100%)', opacity: 0 },
          { transform: 'translateY(-60%)', opacity: 1, offset: 0.1 },
          { transform: 'translateY(' + (h - 46) + 'px)', opacity: 1, offset: 0.92 },
          { transform: 'translateY(' + h + 'px)', opacity: 0 }
        ], { duration: dur, easing: 'linear', fill: 'forwards' });
      }
      rowEls.forEach(function (r, i) {
        later(function () { fixRow(r); }, dur * ((i + 0.7) / rowEls.length) * 0.9);
      });
      later(function () { rowEls[DUP_INDEX].classList.add('dup'); }, dur + 150);
      later(function () { rowEls[DUP_INDEX].classList.remove('dup'); rowEls[DUP_INDEX].classList.add('gone'); }, dur + 750);
      later(function () { setBadge('clean', 'Clean'); sum.textContent = summary(); }, dur + 1150);
      later(function () { if (loop && visible && !document.hidden) play(); }, dur + 1150 + 4200);
    }

    host.addEventListener('click', function () { if (!reduced()) { started = true; play(); } });

    if (reduced()) { showClean(); return; }
    showMessy();
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          visible = en.isIntersecting;
          if (visible && !document.hidden) { if (!started) { started = true; later(play, 900); } else { play(); } }
          else if (!visible) { stop(); }
        });
      }, { threshold: 0.35 }).observe(host);
      document.addEventListener('visibilitychange', function () {
        if (document.hidden) stop(); else if (visible) play();
      });
    } else {
      showClean();
    }
  }

  // ---------------------------------------------------------------- upload button: a gentle nudge if nobody has touched anything yet
  function initNudge() {
    var btn = document.querySelector('#drop .btn-upload');
    if (!btn || reduced()) return;
    var cancelled = false, timer;
    function cancel() { cancelled = true; clearTimeout(timer); btn.classList.remove('nudge'); }
    ['pointerdown', 'keydown', 'focusin', 'dragenter'].forEach(function (t) { document.addEventListener(t, cancel, { once: true, passive: true }); });
    btn.addEventListener('animationend', function () { btn.classList.remove('nudge'); });
    timer = setTimeout(function () {
      if (cancelled || document.hidden || document.body.classList.contains('has-file')) return;
      btn.classList.add('nudge');
    }, 5000);
  }

  // ---------------------------------------------------------------- "Clean my data" lights up once it becomes available
  function initReadyShine() {
    var run = document.getElementById('runBtn');
    if (!run || !('MutationObserver' in window)) return;
    new MutationObserver(function () {
      if (!run.disabled && !run.classList.contains('is-ready') && !reduced()) {
        run.classList.add('is-ready');
        setTimeout(function () { run.classList.remove('is-ready'); }, 1000);
      }
    }).observe(run, { attributes: true, attributeFilter: ['disabled'] });
  }

  // ---------------------------------------------------------------- drop a file anywhere on the page
  // Landing/workspace: hands one file to #fileInput. Pro batch page: hands the files to #batchFiles while the
  // chooser is showing. Either way it fires the input's own 'change' event, the same path as picking by hand.
  function dropTarget() {
    var single = document.getElementById('fileInput');
    if (single) return document.body.classList.contains('has-file') ? null : single;
    var batch = document.getElementById('batchFiles'), setup = document.getElementById('batchSetup');
    if (batch && setup && !setup.classList.contains('hidden')) return batch;
    return null;
  }
  function initDropVeil() {
    if (!document.getElementById('fileInput') && !document.getElementById('batchFiles')) return;
    var veil = el('div', 'drop-veil');
    veil.setAttribute('aria-hidden', 'true');
    var multi = !!document.getElementById('batchFiles');
    veil.innerHTML = '<div class="drop-veil-card"><svg viewBox="0 0 24 24"><path d="M12 16V5M7.5 9.5L12 5l4.5 4.5M5 19h14"/></svg><strong>' + (multi ? 'Drop your files to clean them' : 'Drop your file to clean it') + '</strong><span>CSV, XLSX or XLS</span></div>';
    document.body.appendChild(veil);

    var depth = 0;
    function hasFiles(e) {
      var t = e.dataTransfer && e.dataTransfer.types;
      if (!t) return false;
      for (var i = 0; i < t.length; i++) if (t[i] === 'Files') return true;
      return false;
    }
    function hide() { depth = 0; veil.classList.remove('on'); }
    window.addEventListener('dragenter', function (e) {
      if (!hasFiles(e) || !dropTarget()) return;
      depth++; veil.classList.add('on');
    });
    window.addEventListener('dragover', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();                      // also stops the browser from opening a file dropped outside the box
    });
    window.addEventListener('dragleave', function (e) {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0 || (e.clientX <= 0 && e.clientY <= 0)) hide();
    });
    window.addEventListener('drop', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      hide();
      if (e.target && e.target.closest && e.target.closest('#drop')) return;   // the drop box has its own handler in clean.js
      var input = dropTarget();
      var files = e.dataTransfer.files;
      if (!input || !files || !files.length) return;
      try {
        var dt = new DataTransfer();
        if (input.multiple) Array.prototype.forEach.call(files, function (f) { dt.items.add(f); }); else dt.items.add(files[0]);
        input.files = dt.files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
      } catch (err) {
        if (window.Omixa && window.Omixa.toast) window.Omixa.toast('Use the Choose files button instead.', { type: 'info' });
      }
    });
  }

  // ---------------------------------------------------------------- landing: steps line follows the scroll
  function initSteps() {
    var grid = document.querySelector('.steps3');
    if (!grid) return;
    var cards = Array.prototype.slice.call(grid.children);
    var ticking = false;
    function update() {
      ticking = false;
      var r = grid.getBoundingClientRect(), vh = window.innerHeight;
      var p = clamp((vh * 0.78 - r.top) / Math.max(r.height + vh * 0.1, 1), 0, 1);
      if (reduced()) p = 1;
      grid.style.setProperty('--p', p.toFixed(3));
      cards.forEach(function (c, i) { c.classList.toggle('lit', p >= (i === 0 ? 0.08 : i / (cards.length - 1) * 0.92)); });
    }
    function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(update); } }
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    update();
  }

  // ---------------------------------------------------------------- landing: before/after values resolve letter by letter
  function initBeforeAfter() {
    var card = document.getElementById('baCard'), after = document.getElementById('baAfter');
    if (!card || !after || !('MutationObserver' in window)) return;
    var scheduled = false;
    function run() {
      scheduled = false;
      if (reduced() || !card.classList.contains('is-animating')) return;
      Array.prototype.forEach.call(after.children, function (li, i) {
        var node = li.firstChild;
        if (!node || node.nodeType !== 3 || !li.classList.contains('changed')) return;
        var finalText = node.nodeValue;
        scramble(function (t) { node.nodeValue = t; }, finalText, 420, 420 + i * 110);
      });
    }
    function schedule() { if (!scheduled) { scheduled = true; setTimeout(run, 0); } }
    new MutationObserver(schedule).observe(card, { attributes: true, attributeFilter: ['class'] });
    new MutationObserver(schedule).observe(after, { childList: true });
  }

  // ---------------------------------------------------------------- confetti when cleaning finishes
  function initConfetti() {
    var results = document.getElementById('results');
    if (!results || !('MutationObserver' in window)) return;
    var wasHidden = results.classList.contains('hidden');
    function palette() {
      var cs = getComputedStyle(root), out = [];
      ['--omixa-violet', '--omixa-glow', '--omixa-indigo', '--omixa-violet-light', '--success'].forEach(function (v) {
        var c = cs.getPropertyValue(v).trim(); if (c) out.push(c);
      });
      return out.length ? out : ['#a554f7'];
    }
    function burst() {
      var anchor = document.querySelector('#results .done-check');
      var rect = anchor ? anchor.getBoundingClientRect() : { left: window.innerWidth / 2, top: window.innerHeight / 3, width: 0, height: 0 };
      var ox = rect.left + rect.width / 2, oy = rect.top + rect.height / 2;
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var cv = el('canvas', 'confetti');
      cv.setAttribute('aria-hidden', 'true');
      cv.width = window.innerWidth * dpr; cv.height = window.innerHeight * dpr;
      document.body.appendChild(cv);
      var ctx = cv.getContext('2d');
      if (!ctx) { cv.remove(); return; }
      ctx.scale(dpr, dpr);
      var cols = palette(), parts = [];
      for (var i = 0; i < 56; i++) {
        var a = Math.random() * Math.PI * 2, sp = 4 + Math.random() * 7;
        parts.push({ x: ox, y: oy, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 5, w: 5 + Math.random() * 5, h: 3 + Math.random() * 4,
          r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4, c: cols[i % cols.length], life: 0 });
      }
      var start = null;
      function frame(now) {
        if (start === null) start = now;
        ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
        var alive = false;
        parts.forEach(function (p) {
          p.vy += 0.3; p.vx *= 0.985; p.vy *= 0.985; p.x += p.vx; p.y += p.vy; p.r += p.vr; p.life++;
          var o = clamp(1 - p.life / 110, 0, 1);
          if (o <= 0 || p.y > window.innerHeight + 20) return;
          alive = true;
          ctx.save(); ctx.globalAlpha = o; ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.c; ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); ctx.restore();
        });
        if (alive && now - start < 2600) requestAnimationFrame(frame); else if (cv.parentNode) cv.parentNode.removeChild(cv);
      }
      requestAnimationFrame(frame);
      setTimeout(function () { if (cv.parentNode) cv.parentNode.removeChild(cv); }, 3000);
    }
    new MutationObserver(function () {
      var hiddenNow = results.classList.contains('hidden');
      if (wasHidden && !hiddenNow && !reduced() && !document.hidden) setTimeout(burst, 250);
      wasHidden = hiddenNow;
    }).observe(results, { attributes: true, attributeFilter: ['class'] });
  }

  // ---------------------------------------------------------------- inner pages: heading words
  // Splits the page heading into words so they arrive one by one. A .grad-text span stays one unit
  // (gradient text breaks if its words become separate animated boxes).
  function initPageHeading() {
    if (document.getElementById('tool')) return;
    var h = document.querySelector('.dash-header h1');
    if (!h || h.getAttribute('data-split')) return;
    h.setAttribute('data-split', '1');
    var i = 0;
    Array.prototype.slice.call(h.childNodes).forEach(function (node) {
      if (node.nodeType === 3) {
        var parts = node.nodeValue.split(/(\s+)/), frag = document.createDocumentFragment();
        parts.forEach(function (p) {
          if (!p) return;
          if (/^\s+$/.test(p)) { frag.appendChild(document.createTextNode(' ')); return; }
          var w = el('span', 'w', p); w.style.setProperty('--wi', i++); frag.appendChild(w);
        });
        h.replaceChild(frag, node);
      } else if (node.nodeType === 1) {
        node.classList.add('w'); node.style.setProperty('--wi', i++);
      }
    });
  }

  // ---------------------------------------------------------------- inner pages: cards rise as they scroll into view
  // Covers cards in the template and cards the app renders later (reports). The workspace/landing has its own reveals.
  function initCardReveal() {
    if (document.getElementById('tool') || reduced() || !('IntersectionObserver' in window)) return;
    var main = document.querySelector('.app-main');
    if (!main) return;
    var reported = false, queue = [];
    var io = new IntersectionObserver(function (entries) {
      reported = true;
      var k = 0;
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        en.target.style.setProperty('--rd', Math.min(k++, 5) * 80 + 'ms');
        en.target.classList.add('i-in');
        (function (t) { setTimeout(function () { t.classList.remove('i-rv', 'i-in'); t.style.removeProperty('--rd'); }, 1100); })(en.target);
      });
    }, { threshold: 0.06, rootMargin: '0px 0px -4% 0px' });
    function register(scope) {
      Array.prototype.forEach.call(scope.querySelectorAll('.card:not([data-rv])'), function (c) {
        c.setAttribute('data-rv', '');
        c.classList.add('i-rv');
        io.observe(c);
        queue.push(c);
      });
    }
    register(main);
    if ('MutationObserver' in window) {
      var t = 0;
      new MutationObserver(function () { clearTimeout(t); t = setTimeout(function () { register(main); }, 30); }).observe(main, { childList: true, subtree: true });
    }
    // If the observer never reports in, show everything rather than hide it.
    setTimeout(function () { if (!reported) queue.forEach(function (c) { c.classList.remove('i-rv'); }); }, 2500);
  }

  // ---------------------------------------------------------------- pricing: price numbers count up when they appear
  function initPriceCount() {
    var prices = document.querySelectorAll('.price-card .price');
    if (!prices.length || reduced() || !('IntersectionObserver' in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        var node = en.target.firstChild;
        if (!node || node.nodeType !== 3) return;
        var m = /^(\D*)(\d+(?:\.\d+)?)(\D*)$/.exec(node.nodeValue.trim());
        if (!m) return;
        var target = parseFloat(m[2]), dec = (m[2].split('.')[1] || '').length, finalText = node.nodeValue;
        if (!(target > 0)) return;
        var t0 = null, dur = 700;
        function tick(now) {
          if (t0 === null) t0 = now;
          var p = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - p, 3);
          node.nodeValue = p < 1 ? m[1] + (target * e).toFixed(dec) + m[3] : finalText;
          if (p < 1) requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
        setTimeout(function () { node.nodeValue = finalText; }, dur + 250);
      });
    }, { threshold: 0.5 });
    Array.prototype.forEach.call(prices, function (p) { io.observe(p); });
  }

  // ---------------------------------------------------------------- quality report: score badges count up ("87/100 ...")
  function initScoreCount() {
    var body = document.getElementById('sBody');
    if (!body || !('MutationObserver' in window)) return;
    function run() {
      if (reduced()) return;
      Array.prototype.forEach.call(body.querySelectorAll('.badge:not([data-counted])'), function (b) {
        var m = /^(\d{1,3})(\/100[\s\S]*)$/.exec((b.textContent || '').trim());
        if (!m) return;
        b.setAttribute('data-counted', '1');
        var target = parseInt(m[1], 10), finalText = b.textContent;
        if (target < 2) return;
        var t0 = null, dur = 800;
        function tick(now) {
          if (t0 === null) t0 = now;
          var p = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - p, 3);
          b.textContent = p < 1 ? Math.round(target * e) + m[2] : finalText;
          if (p < 1) requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
        setTimeout(function () { b.textContent = finalText; }, dur + 250);
      });
    }
    var t = 0;
    new MutationObserver(function () { clearTimeout(t); t = setTimeout(run, 120); }).observe(body, { childList: true, subtree: true });
  }

  // ---------------------------------------------------------------- sign-in: show/hide password, shake on error, title swap
  function initAuth() {
    var card = document.querySelector('.auth-card'), pw = document.getElementById('password');
    if (!card || !pw) return;
    var wrap = el('div', 'pw-wrap');
    pw.parentNode.insertBefore(wrap, pw);
    wrap.appendChild(pw);
    var btn = el('button', 'pw-toggle', 'Show');
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('aria-pressed', 'false');
    btn.addEventListener('click', function () {
      var show = pw.type === 'password';
      pw.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      pw.focus();
    });
    wrap.appendChild(btn);

    if (!('MutationObserver' in window) || reduced()) return;
    var msg = document.getElementById('loginMsg'), title = document.getElementById('loginTitle');
    if (msg) new MutationObserver(function () {
      if (!(msg.textContent || '').trim()) return;
      card.classList.remove('shake'); void card.offsetWidth; card.classList.add('shake');
      setTimeout(function () { card.classList.remove('shake'); }, 520);
    }).observe(msg, { childList: true, characterData: true, subtree: true });
    if (title) new MutationObserver(function () {
      title.classList.remove('swap'); void title.offsetWidth; title.classList.add('swap');
    }).observe(title, { childList: true, characterData: true, subtree: true });
  }

  // ---------------------------------------------------------------- go
  function init() {
    root.classList.add('i-ready');
    initHeadline();
    initAmbient();
    initPointerEffects();
    initRipple();
    initDemo();
    initNudge();
    initReadyShine();
    initDropVeil();
    initSteps();
    initBeforeAfter();
    initConfetti();
    initPageHeading();
    initCardReveal();
    initPriceCount();
    initScoreCount();
    initAuth();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
