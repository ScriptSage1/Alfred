(function () {
  'use strict';
  var root = document.documentElement;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- Theme: follows Windows unless the visitor picks one ---------- */
  var toggle = document.getElementById('theme-toggle');
  function effectiveTheme() {
    if (root.dataset.theme) return root.dataset.theme;
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  if (toggle) toggle.addEventListener('click', function () {
    var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('juno-theme', next); } catch (e) {}
  });

  /* ---------- Nav gets more solid once the page scrolls ---------- */
  var nav = document.querySelector('.nav');
  function onScroll() { if (nav) nav.classList.toggle('scrolled', window.scrollY > 24); }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ---------- Reveal on scroll ---------- */
  var reveals = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && !reduceMotion) {
    var ro = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); ro.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.12 });
    reveals.forEach(function (el) { ro.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add('in'); });
  }

  /* ---------- Videos: load when near the screen, play only while visible ---------- */
  var ICONS = '<svg class="i-pause" viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="2.5" width="3" height="11" rx="1"/><rect x="9.5" y="2.5" width="3" height="11" rx="1"/></svg>' +
              '<svg class="i-play" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.8v10.4a.6.6 0 0 0 .9.5l8.3-5.2a.6.6 0 0 0 0-1L5.4 2.3a.6.6 0 0 0-.9.5z"/></svg>';
  var videos = Array.prototype.slice.call(document.querySelectorAll('video.lazy-video'));

  function load(v) {
    if (v.dataset.loaded) return;
    v.querySelectorAll('source[data-src]').forEach(function (s) { s.src = s.dataset.src; });
    v.load();
    v.dataset.loaded = '1';
  }
  function setPaused(v, paused) {
    var frame = v.closest('.frame');
    if (frame) frame.classList.toggle('paused', paused);
    var btn = frame && frame.querySelector('.play-state');
    if (btn) btn.setAttribute('aria-label', paused ? 'Play video' : 'Pause video');
  }
  function play(v) {
    load(v);
    var p = v.play();
    if (p && p.catch) p.catch(function () { setPaused(v, true); });
  }
  function userToggle(v) {
    if (v.paused) { v.dataset.userPaused = ''; play(v); }
    else { v.dataset.userPaused = '1'; v.pause(); }
  }

  videos.forEach(function (v) {
    var frame = v.closest('.frame');
    if (frame) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'play-state';
      btn.innerHTML = ICONS;
      btn.addEventListener('click', function (e) { e.stopPropagation(); userToggle(v); });
      frame.appendChild(btn);
    }
    v.addEventListener('click', function () { userToggle(v); });
    v.addEventListener('play', function () { setPaused(v, false); });
    v.addEventListener('pause', function () { setPaused(v, true); });
    if (reduceMotion) { v.dataset.userPaused = '1'; setPaused(v, true); }
  });

  if ('IntersectionObserver' in window) {
    // Start fetching a little before a clip scrolls in.
    var preload = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { load(e.target); preload.unobserve(e.target); } });
    }, { rootMargin: '300px 0px' });
    // Play while at least a third is on screen; pause otherwise to save battery.
    var vis = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        var v = e.target;
        if (e.isIntersecting && !v.dataset.userPaused) play(v);
        else if (!e.isIntersecting && !v.paused) v.pause();
      });
    }, { threshold: 0.35 });
    videos.forEach(function (v) { preload.observe(v); vis.observe(v); });
  } else {
    videos.forEach(function (v) { load(v); if (!reduceMotion) play(v); });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) videos.forEach(function (v) { if (!v.paused) { v.dataset.wasPlaying = '1'; v.pause(); } });
    else videos.forEach(function (v) { if (v.dataset.wasPlaying) { delete v.dataset.wasPlaying; play(v); } });
  });

  /* ---------- Download: point at the newest Windows zip on GitHub Releases ----------
     The links in the HTML already work without this. GitHub's "latest" release can be a non-app release (the model
     files), so instead of /releases/latest this looks through recent releases for the newest Juno-*-win64.zip. */
  var REPO = 'ScriptSage1/Juno';
  var ASSET = /^Juno-(\d+(?:\.\d+)*)-win64\.zip$/;
  function applyRelease(info) {
    document.querySelectorAll('.js-download').forEach(function (a) { a.href = info.url; });
    document.querySelectorAll('.js-release-page').forEach(function (a) { a.href = info.page; });
    var mb = Math.round(info.size / 1e6);
    var label = 'Juno ' + info.version + (info.prerelease ? ' test build' : '') + ' · 64-bit zip, about ' + mb + ' MB';
    document.querySelectorAll('.js-download-meta').forEach(function (p, i) {
      p.textContent = i === 0 ? label + ' · no install or admin rights' : label;
    });
  }
  function pick(releases) {
    for (var i = 0; i < releases.length; i++) {
      var r = releases[i];
      if (r.draft) continue;
      for (var j = 0; j < (r.assets || []).length; j++) {
        var m = ASSET.exec(r.assets[j].name);
        if (m) return { url: r.assets[j].browser_download_url, size: r.assets[j].size, version: m[1], prerelease: r.prerelease, page: r.html_url };
      }
    }
    return null;
  }
  try {
    var cached = sessionStorage.getItem('juno-release');
    if (cached) { applyRelease(JSON.parse(cached)); return; }
  } catch (e) {}
  if (!window.fetch) return;
  fetch('https://api.github.com/repos/' + REPO + '/releases?per_page=15', { headers: { Accept: 'application/vnd.github+json' } })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (list) {
      var info = pick(list);
      if (!info) return;
      applyRelease(info);
      try { sessionStorage.setItem('juno-release', JSON.stringify(info)); } catch (e) {}
    })
    .catch(function () { /* keep the links written in the HTML */ });
})();
