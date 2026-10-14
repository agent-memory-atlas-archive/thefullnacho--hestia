// Today view: what is due, read straight from the stores by GET /today. No model, no GPU.
// Each section can fail on its own (HA down, calendar unreachable) without blanking the rest.
'use strict';

(() => {
  const body = () => document.getElementById('todayBody');

  const time = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  };
  const failed = (sec) => sec && sec.error ? '<p class="t-err">' + esc(sec.error) + '</p>' : '';
  const card = (title, inner) => '<div class="card"><h2>' + esc(title) + '</h2>' + inner + '</div>';
  const list = (rows, none) => rows.length ? '<ul class="t-list">' + rows.join('') + '</ul>' : '<p class="t-none">' + esc(none) + '</p>';

  function render(t) {
    let h = '';

    const rem = t.reminders || {};
    h += card('Reminders', failed(rem) || list((rem.items || []).map(r =>
      '<li><span>' + esc(r.text) + '</span><span class="when' + (r.overdue ? ' late' : '') + '">' +
      (r.overdue ? 'overdue · ' : '') + time(r.due_at) + '</span></li>'), 'Nothing due today.'));

    const cal = t.calendar || {};
    h += card('Calendar', failed(cal) || list((cal.events || []).map(e =>
      '<li><span>' + esc(e.summary) + (e.location ? ' <span class="muted">· ' + esc(e.location) + '</span>' : '') +
      '</span><span class="when">' + (e.all_day ? 'all day' : time(e.start)) + '</span></li>'), 'Nothing on the calendar.'));

    const q = t.queue || {};
    if (q.error) h += card('House and garden', failed(q));
    else {
      const cols = q.columns || {};
      const home = (cols.home || []).map(i => '<li><span>' + esc(i.title) + '</span><span class="when">' + esc(i.sub) + '</span></li>');
      const hands = (cols.hands || []).map(i => '<li><span>' + esc(i.title) + '</span><span class="when">' + esc(i.sub) + '</span></li>');
      h += card('House and garden', (q.headline ? '<p style="margin:0 0 10px">' + esc(q.headline) + '</p>' : '') +
        list(home, 'All quiet.'));
      h += card('Chores', list(hands, 'No chores queued.') +
        '<p class="meta" style="margin-top:10px"><a href="/queue" data-capture-link>Open the queue</a> to mark things done.</p>');
    }

    const m = t.maintenance || {};
    h += card('Maintenance', failed(m) || list((m.assets || []).map(a =>
      '<li><span>' + esc(a.name) + '</span><span class="when late">' +
      esc(a.reason || (a.days_since != null ? a.days_since + 'd overdue' : 'due')) + '</span></li>'), 'All assets current.'));

    const shop = t.shopping || {};
    h += card('Shopping list', failed(shop) || list((shop.items || []).map(i => '<li><span>' + esc(i) + '</span></li>'), 'The list is empty.'));

    h += '<div class="foot-note">' + esc(t.date || '') + ' · read at ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) + '</div>';
    body().innerHTML = h;
    // The queue link carries the NFC token when this phone has one, the same as the menu entry.
    body().querySelector('[data-capture-link]')?.addEventListener('click', (e) => {
      const token = getToken('nfc', { ask: false });
      if (token) { e.preventDefault(); location.href = '/queue?token=' + encodeURIComponent(token); }
    });
  }

  async function refresh() {
    try {
      const r = await fetch('/today');
      if (!r.ok) throw new Error('today ' + r.status);
      render(await r.json());
    } catch (e) {
      body().innerHTML = '<div class="empty">Couldn’t load today: ' + esc(e.message) +
        '. Is the brain up and are you on the tailnet?</div>';
    }
  }

  Hestia.views.today = { title: 'Today', enter: refresh, resume: refresh };
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('todayRefresh').addEventListener('click', refresh);
  });
})();
