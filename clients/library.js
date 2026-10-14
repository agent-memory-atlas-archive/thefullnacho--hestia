// Library view: the one clips renderer (/clips now redirects here) plus the way into recipes.
// Same /api/clips feed as before. The picks card above it is feed.js.
'use strict';

(() => {
  let items = [], tag = '', needsOnly = false;
  const $ = (id) => document.getElementById(id);

  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  async function load() {
    const p = new URLSearchParams();
    if (tag) p.set('tag', tag);
    if (needsOnly) p.set('needs_note', '1');
    try {
      const r = await fetch('/api/clips' + (p.toString() ? '?' + p : ''));
      if (!r.ok) throw new Error('clips ' + r.status);
      items = (await r.json()).clips || [];
      renderChips();
      renderGrid();
    } catch (e) {
      $('clipGrid').replaceChildren();
      $('clipChips').replaceChildren();
      $('clipEmpty').hidden = false;
      $('clipEmpty').textContent = 'Couldn’t load clips: ' + e.message + '. Is the brain up and are you on the tailnet?';
    }
  }

  function chip(label, pressed, extra = '') {
    return '<button type="button" class="chip' + extra + '" aria-pressed="' + pressed + '"' + '>' + esc(label) + '</button>';
  }

  function renderChips() {
    const tags = [...new Set(items.flatMap(c => (c.tags || '').split(',').map(t => t.trim()).filter(Boolean)))].sort();
    const el = $('clipChips');
    el.innerHTML = chip('All', !tag && !needsOnly) + chip('Needs a note', needsOnly, ' warn') +
      tags.map(t => chip(t, tag === t)).join('');
    const buttons = el.querySelectorAll('.chip');
    buttons[0].addEventListener('click', () => { tag = ''; needsOnly = false; load(); });
    buttons[1].addEventListener('click', () => { needsOnly = !needsOnly; tag = ''; load(); });
    tags.forEach((t, i) => buttons[i + 2].addEventListener('click', () => {
      tag = tag === t ? '' : t; needsOnly = false; load();
    }));
  }

  function renderGrid() {
    $('clipEmpty').hidden = items.length > 0;
    $('clipEmpty').textContent = 'No clips yet. Save one from the Share Sheet shortcut.';
    const grid = $('clipGrid');
    grid.replaceChildren();
    for (const c of items) {
      const wrap = document.createElement('div');
      wrap.className = 'clip-player';
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'clip';
      const media = c.thumb_url
        ? '<img class="thumb" src="' + esc(c.thumb_url) + '" loading="lazy" alt="">'
        : '<span class="thumb">' + (c.is_audio ? ICONS.speakerOn : ICONS.clips) + '</span>';
      card.innerHTML = media + '<span class="body" style="display:block">' +
        (c.note ? '<span class="note" style="display:block">' + esc(c.note) + '</span>'
                : '<span class="note none" style="display:block">No note yet</span>') +
        '<span class="meta" style="display:block">' + esc(c.sender || '') + ' · ' + fmtDate(c.taken_at || c.created_at) +
        (c.needs_note ? '<span class="badge">needs a note</span>' : '') + '</span></span>';
      card.setAttribute('aria-label', 'Play clip' + (c.note ? ': ' + c.note : '') + ', ' + fmtDate(c.taken_at || c.created_at));
      card.addEventListener('click', () => play(c, wrap, card));
      wrap.appendChild(card);
      grid.appendChild(wrap);
    }
  }

  function play(c, wrap, card) {
    if (!c.file_url) return;
    const old = wrap.querySelector('video, audio');
    if (old) { old.remove(); return; }
    const el = document.createElement(c.is_audio ? 'audio' : 'video');
    el.src = c.file_url;
    el.controls = true;
    el.playsInline = true;
    wrap.insertBefore(el, card);
    el.play().catch(() => {});
  }

  Hestia.views.library = { title: 'Library', enter: () => { Hestia.feed?.load(); load(); } };
})();
