// Category Search (client-side filtering) — Roundups hub
(function () {
  function qs(s, ctx) { return (ctx || document).querySelector(s); }
  function qsa(s, ctx) { return Array.from((ctx || document).querySelectorAll(s)); }

  const input   = qs('#categorySearch');
  const clearBtn= qs('#clearSearch');
  const grid    = qs('#catGrid') || document;
  const countEl = qs('#searchCount');
  const noRes   = qs('#noResults');

  let cards = qsa('#catGrid [data-cat]', grid);
  if (!cards.length) cards = qsa('#catGrid .roundups-card', grid);
  if (!cards.length) cards = qsa('#catGrid a, #catGrid article, #catGrid .card', grid);

  function normalize(s){ return (s||'').toLowerCase().trim(); }
  function textOfCard(card){
    const d = card.getAttribute('data-cat') || card.getAttribute('aria-label') || '';
    const text = (d ? d + ' ' : '') + (card.textContent || '');
    return normalize(text);
  }

  const index = cards.map(card => ({ card, text: textOfCard(card) }));

  function apply(q){
    const nq = normalize(q);
    let shown = 0;
    index.forEach(({ card, text }) => {
      const match = !nq || text.includes(nq);
      card.style.display = match ? '' : 'none';
      if (match) shown++;
    });
    if (countEl) countEl.textContent = shown + ' of ' + index.length + ' categories';
    if (noRes) noRes.hidden = shown !== 0;
  }

  if (!input) return;

  input.addEventListener('input', e => apply(e.target.value));
  if (clearBtn) clearBtn.addEventListener('click', () => { input.value=''; input.focus(); apply(''); });

  // Keyboard niceties
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
    if (e.key === '/' && !/input|textarea|select/.test(tag)) { e.preventDefault(); input.focus(); }
    else if (e.key === 'Escape') { input.value=''; apply(''); }
  });

  // Prefill from ?q=
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q') || '';
  if (q) input.value = q;

  apply(input.value);
})();
