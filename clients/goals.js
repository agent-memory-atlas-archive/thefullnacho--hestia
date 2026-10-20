// Goals view: drafts Hestia proposed, waiting for a person; active goals with tappable steps.
// Approving, discarding and ticking are plain POSTs to /api/goals. No model in any of it.
'use strict';

(() => {
  const body = () => document.getElementById('goalsBody');
  const HEADERS = { 'Content-Type': 'application/json', 'X-Hestia-Goals': '1' };
  let data = { goals: [], done: [] };

  const today = () => new Date().toISOString().slice(0, 10);
  const nice = (iso) => {
    if (!iso) return '';
    const d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  };
  const card = (inner) => '<div class="card">' + inner + '</div>';

  function draftCard(g) {
    const steps = g.steps.map(s => stepEditor(s.id, s.text, s.due)).join('');
    return '<div class="card g-draft" data-goal="' + g.id + '">' +
      '<h2>Draft from Hestia</h2>' +
      '<label class="g-label">Goal<input class="g-input" data-f="title" value="' + esc(g.title) + '"></label>' +
      '<label class="g-label">Why<input class="g-input" data-f="why" value="' + esc(g.why || '') + '"></label>' +
      '<label class="g-label">Aim for<input class="g-input" type="date" data-f="target" value="' + esc(g.target || '') + '"></label>' +
      '<div class="g-label">Steps</div><ol class="g-edit">' + steps + '</ol>' +
      '<button class="pill-btn" data-act="add">Add step</button>' +
      '<p class="meta">Nothing is scheduled until you approve. Dated steps ping your phone at 9am that day.</p>' +
      '<p class="t-err" data-err hidden></p>' +
      '<div class="sheet-actions"><button class="btn" data-act="discard">Discard</button>' +
      '<button class="btn primary" data-act="approve">Approve</button></div></div>';
  }

  function stepEditor(id, text, due) {
    return '<li data-step="' + (id ?? '') + '">' +
      '<input class="g-input" data-s="text" value="' + esc(text || '') + '" placeholder="What to do">' +
      '<input class="g-input g-date" type="date" data-s="due" value="' + esc(due || '') + '" aria-label="Due">' +
      '<button class="icon-btn g-del" data-act="del" aria-label="Remove step" data-icon="close"></button></li>';
  }

  function activeCard(g) {
    const pct = g.steps.length ? Math.round(100 * g.done / g.steps.length) : 0;
    const now = today();
    const rows = g.steps.map(s => {
      const late = !s.done_at && s.due && s.due < now;
      const when = s.done_at ? 'done' : s.due ? (late ? 'overdue · ' : '') + nice(s.due) : '';
      return '<li class="g-step' + (s.done_at ? ' is-done' : '') + '">' +
        '<button class="g-check" data-tick="' + s.id + '" data-done="' + (s.done_at ? 1 : 0) + '" aria-pressed="' +
        (s.done_at ? 'true' : 'false') + '" aria-label="' + (s.done_at ? 'Mark not done: ' : 'Mark done: ') + esc(s.text) + '">' +
        (s.done_at ? ICONS.check : '') + '</button>' +
        '<span class="g-text">' + esc(s.text) + '</span>' +
        '<span class="when' + (late ? ' late' : '') + '">' + esc(when) + '</span></li>';
    }).join('');
    return card(
      '<h2>' + esc(g.title) + '</h2>' +
      (g.why ? '<p class="meta" style="margin:-4px 0 8px">' + esc(g.why) + '</p>' : '') +
      '<div class="metric"><span class="k">' + g.done + ' of ' + g.steps.length + ' steps</span>' +
      '<span class="k">' + (g.target ? 'aim ' + esc(nice(g.target)) : '') + '</span></div>' +
      '<div class="bar"><i style="width:' + pct + '%"></i></div>' +
      '<ul class="t-list g-steps">' + rows + '</ul>');
  }

  function render() {
    const drafts = data.goals.filter(g => g.status === 'proposed');
    const active = data.goals.filter(g => g.status === 'active');
    let h = '';
    h += drafts.map(draftCard).join('');
    h += active.map(activeCard).join('');
    if (!drafts.length && !active.length) {
      h += '<div class="empty">No goals yet. Tell Hestia in chat what you want done and by when, ' +
        'and the plan she drafts will show up here for you to approve.</div>';
    }
    if (data.done.length) {
      h += card('<h2>Done</h2><ul class="t-list">' + data.done.map(g =>
        '<li><span>' + esc(g.title) + '</span><span class="when">' + g.steps.length + ' steps</span></li>').join('') + '</ul>');
    }
    body().innerHTML = h;
  }

  async function post(path, payload) {
    const r = await fetch(path, { method: 'POST', headers: HEADERS, body: JSON.stringify(payload || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }

  function collect(el) {
    const f = (n) => el.querySelector('[data-f="' + n + '"]').value.trim();
    const steps = [...el.querySelectorAll('.g-edit li')].map(li => ({
      id: li.dataset.step ? Number(li.dataset.step) : null,
      text: li.querySelector('[data-s="text"]').value.trim(),
      due: li.querySelector('[data-s="due"]').value || null,
    })).filter(s => s.text);
    return { title: f('title'), why: f('why'), target: f('target') || null, steps };
  }

  async function onClick(e) {
    const btn = e.target.closest('button');
    if (!btn) return;
    const draft = btn.closest('.g-draft');
    try {
      if (btn.dataset.tick) {
        btn.disabled = true;
        const j = await post('/api/goals/steps/' + btn.dataset.tick + '/' + (btn.dataset.done === '1' ? 'undo' : 'done'));
        data.goals = data.goals.map(g => g.id === j.goal.id ? j.goal : g);
        if (j.goal.status === 'done') { data.goals = data.goals.filter(g => g.id !== j.goal.id); data.done.unshift(j.goal); }
        render();
        return;
      }
      if (!draft) return;
      const act = btn.dataset.act;
      if (act === 'add') {
        draft.querySelector('.g-edit').insertAdjacentHTML('beforeend', stepEditor(null, '', ''));
        hydrateIcons(draft);
        draft.querySelector('.g-edit li:last-child input').focus();
      } else if (act === 'del') {
        btn.closest('li').remove();
      } else if (act === 'discard') {
        if (!confirm('Discard this draft?')) return;
        await post('/api/goals/' + draft.dataset.goal + '/discard');
        await refresh();
      } else if (act === 'approve') {
        btn.disabled = true;
        await post('/api/goals/' + draft.dataset.goal + '/approve', collect(draft));
        await refresh();
      }
    } catch (err) {
      btn.disabled = false;
      const box = draft?.querySelector('[data-err]');
      if (box) { box.textContent = err.message; box.hidden = false; } else alert(err.message);
    }
  }

  function hydrateIcons(root) {
    root.querySelectorAll('[data-icon]').forEach(el => { if (!el.innerHTML.trim()) el.innerHTML = ICONS[el.dataset.icon] || ''; });
  }

  async function refresh() {
    try {
      const r = await fetch('/api/goals');
      if (!r.ok) throw new Error('goals ' + r.status);
      data = await r.json();
      render();
      hydrateIcons(body());
    } catch (e) {
      body().innerHTML = '<div class="empty">Couldn’t load goals: ' + esc(e.message) +
        '. Is the brain up and are you on the tailnet?</div>';
    }
  }

  Hestia.views.goals = { title: 'Goals', enter: refresh, resume: refresh };
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('goalsRefresh').addEventListener('click', refresh);
    body().addEventListener('click', onClick);
  });
})();
