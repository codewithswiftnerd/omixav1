// Home page extras: the Before/After tabs in the "Professional quality"
// section. Every example here is real Omixa behavior (checked against
// the cleaning pipeline), not an illustration.
(function () {
  var DATA = {
    dates: {
      before: ['March 14, 2024', '14-Mar-2024', '2024/3/9', '2024-03-14'],
      after:  ['2024-03-14', '2024-03-14', '2024-03-09', '2024-03-14']
    },
    phones: {
      before: ['(0803) 123-4567', '0803.123.4568', '0712 345 678', '0722-555-019'],
      after:  ['08031234567', '08031234568', '0712345678', '0722555019']
    },
    countries: {
      before: ['ng', 'nigeria', 'KE', 'Kenya'],
      after:  ['Nigeria', 'Nigeria', 'Kenya', 'Kenya']
    },
    emails: {
      before: [' ADA@Mail.COM ', 'Tunde@MAIL.com', 'kofi@mail.com ', 'CHIDI@MAIL.COM'],
      after:  ['ada@mail.com', 'tunde@mail.com', 'kofi@mail.com', 'chidi@mail.com']
    },
    gender: {
      before: ['F', 'MALE', 'female', 'm'],
      after:  ['female', 'male', 'female', 'male']
    }
  };

  var before = document.getElementById('baBefore');
  var after = document.getElementById('baAfter');
  var tabs = document.querySelectorAll('#qTabs .tab');
  if (!before || !after || !tabs.length) return;

  function row(text, i, cls, tag) {
    var li = document.createElement('li');
    li.style.setProperty('--i', i);
    if (cls) li.className = cls;
    li.appendChild(document.createTextNode(text));
    if (tag) { var t = document.createElement('span'); t.className = 'ba-tag'; t.textContent = tag; li.appendChild(t); }
    return li;
  }
  function render(key) {
    var d = DATA[key];
    if (!d) return;
    before.innerHTML = '';
    after.innerHTML = '';
    d.before.forEach(function (t, i) { before.appendChild(row(t, i)); });
    // A row is "changed" only if the cleaned value really differs from the original (comparison is exact).
    d.after.forEach(function (t, i) {
      var changed = t !== d.before[i];
      after.appendChild(row(t, i, changed ? 'changed' : 'same', changed ? '' : 'unchanged'));
    });
  }

  // Replays the before -> after sequence (CSS does the movement; see .is-animating in motion.css).
  var card = document.getElementById('baCard');
  function play() {
    if (!card) return;
    card.classList.remove('is-animating');
    void card.offsetWidth;                  // restart the CSS animation
    card.classList.add('is-animating');
  }

  function select(tab, focus) {
    tabs.forEach(function (t) { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); t.tabIndex = -1; });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    tab.tabIndex = 0;
    if (focus) tab.focus();
    render(tab.dataset.tab);
    play();
  }
  tabs.forEach(function (tab, i) {
    tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
    tab.addEventListener('click', function () { select(tab, false); });
    tab.addEventListener('keydown', function (e) {
      var next = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : -1;
      if (next < 0) return;
      e.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], true);
    });
  });
  render('dates');
  if (card && 'IntersectionObserver' in window) {
    var seen = new IntersectionObserver(function (entries) {
      if (entries.some(function (e) { return e.isIntersecting; })) { play(); seen.disconnect(); }
    }, { threshold: 0.4 });
    seen.observe(card);
  }

  // "Upload file" at the bottom scrolls back up and opens the picker.
  var cta = document.getElementById('ctaTop');
  var input = document.getElementById('fileInput');
  if (cta && input) {
    cta.addEventListener('click', function (e) {
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(function () { input.click(); }, 350);
    });
  }
})();
