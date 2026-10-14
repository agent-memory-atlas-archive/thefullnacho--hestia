// Feed: tonight's handful of picks for whoever is using this phone, at the top of Library.
// The brain's nightly job did the finding and ranking; this only reads /v1/feed and sends
// hearts and "not for me" taps back. Who's using the phone is a per-device choice, not identity.
'use strict';

(() => {
  const KEY = 'hestia.feedPerson';
  let people = [];
  const $ = (id) => document.getElementById(id);

  async function load() {
    try {
      const r = await fetch('/v1/feed/people');
      if (!r.ok) throw new Error('people ' + r.status);
      people = (await r.json()).people || [];
    } catch (e) {
      people = [];
      $('feedPicker').replaceChildren();
      return message('Couldn’t load the feed: ' + e.message + '.');
    }
    $('feedCard').hidden = people.length === 0;
    if (!people.length) return;
    let who = store.get(KEY);
    if (!people.some(p => p.id === who)) who = people.length === 1 ? people[0].id : null;
    renderPicker(who);
    if (!who) return message('Who’s using this phone? Pick a name to see their picks.');
    await loadItems(who);
  }

  function renderPicker(who) {
    const el = $('feedPicker');
    el.innerHTML = people.map(p =>
      '<label class="chip radio"><input type="radio" name="feedPerson" value="' + esc(p.id) + '"' +
      (p.id === who ? ' checked' : '') + '><span>' + esc(p.name) + '</span></label>').join('');
    el.querySelectorAll('input').forEach(input => input.addEventListener('change', () => {
      store.set(KEY, input.value);
      loadItems(input.value);
    }));
  }

  function message(text) {
    $('feedList').replaceChildren();
    $('feedEmpty').hidden = false;
    $('feedEmpty').textContent = text;
  }

  async function loadItems(who) {
    try {
      const r = await fetch('/v1/feed?person=' + encodeURIComponent(who));
      if (!r.ok) throw new Error('feed ' + r.status);
      const day = await r.json();
      render(day.items || []);
    } catch (e) {
      message('Couldn’t load the feed: ' + e.message + '.');
    }
  }

  function render(items) {
    const list = $('feedList');
    list.replaceChildren();
    $('feedEmpty').hidden = items.length > 0;
    $('feedEmpty').textContent = 'Nothing yet. Picks arrive overnight.';
    for (const it of items) list.appendChild(card(it));
  }

  function card(it) {
    const el = document.createElement('article');
    el.className = 'pick';
    el.innerHTML =
      (it.image_url ? '<img class="pick-img" src="' + esc(it.image_url) + '" loading="lazy" alt="">' : '') +
      '<div class="pick-body">' +
      '<a class="pick-title" href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">' + esc(it.title) + '</a>' +
      '<p class="pick-why">' + esc(it.why) + '</p>' +
      '<div class="pick-foot"><span class="pick-src">' + esc(it.source) + '</span>' +
      '<button type="button" class="pill-btn heart" aria-label="Heart: ' + esc(it.title) + '">' + ICONS.heart + '</button>' +
      '<button type="button" class="pill-btn nope">Not for me</button></div></div>';
    const heart = el.querySelector('.heart'), nope = el.querySelector('.nope');
    const paint = () => {
      heart.setAttribute('aria-pressed', String(it.reaction === 'heart'));
      nope.setAttribute('aria-pressed', String(it.reaction === 'nope'));
      nope.textContent = it.reaction === 'nope' ? 'Undo' : 'Not for me';
      el.classList.toggle('waved', it.reaction === 'nope');
    };
    const send = async (want) => {
      const next = it.reaction === want ? null : want;
      const prev = it.reaction;
      it.reaction = next;
      paint();
      try {
        const r = await fetch('/v1/feed/' + it.id + '/react', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reaction: next }) });
        if (!r.ok) throw new Error(r.status);
        announce(next === 'heart' ? 'Hearted' : next === 'nope' ? 'Marked not for me' : 'Cleared');
      } catch {
        it.reaction = prev;
        paint();
        announce('Couldn’t save that. Try again.');
      }
    };
    heart.addEventListener('click', () => send('heart'));
    nope.addEventListener('click', () => send('nope'));
    paint();
    return el;
  }

  Hestia.feed = { load };
})();
