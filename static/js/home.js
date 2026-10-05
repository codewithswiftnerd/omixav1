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

  function render(key) {
    var d = DATA[key];
    if (!d) return;
    before.innerHTML = '';
    after.innerHTML = '';
    d.before.forEach(function (t) { var li = document.createElement('li'); li.textContent = t; before.appendChild(li); });
    d.after.forEach(function (t) { var li = document.createElement('li'); li.textContent = t; after.appendChild(li); });
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (t) { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      render(tab.dataset.tab);
    });
  });
  render('dates');

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
