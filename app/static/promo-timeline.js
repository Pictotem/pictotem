// Timeline visuelle 24h des fréquences des pages promo (voir
// templates/admin_slideshow.html → section "Pages promo" → "Timeline des
// fréquences"). Une ligne par page promo, un bloc coloré par plage horaire
// (voir promo_page_schedules, db.py) positionné en pourcentage de 1440
// minutes. Chaque bloc peut être redimensionné (poignées gauche/droite) ou
// déplacé (glisser le centre) directement ici : au relâchement, une requête
// AJAX (POST ajax=1) met à jour promo_page_schedules et les petits champs
// heure/heure du formulaire de la page concernée (voir
// #promo-schedule-row-<id> dans le template), sans recharger la page.
//
// Ce fichier ne dépend d'aucun autre script de la page (contrairement à
// promo-editor.js, qui pilote CKEditor) — il lit ses données depuis
// #promo-timeline-data (JSON, voir admin_slideshow.html) exactement comme
// #promo-editor-config pour l'éditeur.
(function () {
  const DATA_EL = document.getElementById('promo-timeline-data');
  const ROWS_EL = document.getElementById('promo-timeline-rows');
  const RULER_EL = document.getElementById('promo-timeline-ruler');
  if (!DATA_EL || !ROWS_EL) return;

  let cfg = { pages: [], schedules: [] };
  try { cfg = JSON.parse(DATA_EL.textContent); } catch (e) { /* défauts ci-dessus */ }

  // Palette cyclique par page (index dans la liste, pas par id — stable tant
  // que l'ordre de passage ne change pas, cohérent avec le rendu des cartes
  // ci-dessous, qui suit le même ordre). Choisie pour rester lisible sur le
  // fond sombre de l'admin.
  const PALETTE = ['#0d8b8f', '#c96b3f', '#7b61ff', '#e0b64f', '#4ecca0',
                    '#e57373', '#5aa9e6', '#c987d6', '#9fbf3f', '#e08fb0'];

  const MIN_DURATION = 15;   // durée minimale d'un créneau (minutes), au glisser
  const SNAP = 5;            // granularité du glisser (minutes)
  const DAY = 1440;          // minutes dans une journée

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  function pad2(n) { return String(n).padStart(2, '0'); }
  function toHHMM(minutes) {
    const m = Math.max(0, Math.min(DAY, Math.round(minutes)));
    return pad2(Math.floor(m / 60) % 24) + ':' + pad2(m % 60);
  }

  function persistSchedule(schedule) {
    // Requête de fond (fire-and-forget, comme postForm dans admin-blocks.js)
    // -- l'UI est déjà à jour de façon optimiste (voir applyPosition
    // ci-dessous), aucune réponse à attendre pour continuer.
    const url = `/admin/slideshow/promo/${schedule.page_id}/schedule/${schedule.id}/update`;
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        _csrf_token: csrfToken(),
        ajax: '1',
        start: toHHMM(schedule.start_minutes),
        end: toHHMM(schedule.end_minutes),
      }),
    }).catch(() => { /* la page se resynchronisera au prochain rechargement */ });
  }

  // Répercute un créneau modifié sur son formulaire d'édition (sous la page
  // promo concernée, voir #promo-schedule-row-<id> côté template) pour que
  // les deux vues (timeline + liste) restent cohérentes sans recharger.
  function syncFormRow(schedule) {
    const row = document.getElementById('promo-schedule-row-' + schedule.id);
    if (!row) return;
    const startInput = row.querySelector('.promo-schedule-start-input');
    const endInput = row.querySelector('.promo-schedule-end-input');
    if (startInput) startInput.value = toHHMM(schedule.start_minutes);
    if (endInput) endInput.value = toHHMM(schedule.end_minutes);
  }

  function buildRuler() {
    if (!RULER_EL) return;
    RULER_EL.innerHTML = '';
    for (let h = 0; h <= 24; h += 2) {
      const tick = document.createElement('span');
      tick.className = 'promo-timeline-tick';
      tick.style.left = (h / 24 * 100) + '%';
      tick.textContent = pad2(h % 24) + 'h';
      RULER_EL.appendChild(tick);
    }
  }

  function segmentLabel(freq) { return '1/' + freq; }

  // Un créneau [start,end[ traversant minuit (end < start) est rendu en deux
  // tronçons non chevauchants : [start,1440[ et [0,end[. Les deux portent le
  // même data-schedule-id mais sont marqués .is-wrap (pas de glisser fiable
  // sur une plage coupée en deux visuellement -- reste éditable via le
  // formulaire de la page, voir admin_slideshow.html).
  function segmentPieces(sch) {
    if (sch.end_minutes > sch.start_minutes) {
      return [{ left: sch.start_minutes, right: sch.end_minutes, wrap: false }];
    }
    if (sch.end_minutes < sch.start_minutes) {
      return [
        { left: sch.start_minutes, right: DAY, wrap: true },
        { left: 0, right: sch.end_minutes, wrap: true },
      ];
    }
    return []; // plage dégénérée (start == end) : rien à afficher
  }

  function renderRows() {
    ROWS_EL.innerHTML = '';
    if (!cfg.pages.length) {
      ROWS_EL.innerHTML = '<p class="promo-timeline-empty">Aucune page promo — créez-en une ci-dessous pour voir sa timeline.</p>';
      return;
    }

    const sorted = [...cfg.pages].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id);
    const nowMinutes = (() => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); })();

    sorted.forEach((page, pageIndex) => {
      const color = PALETTE[pageIndex % PALETTE.length];
      const row = document.createElement('div');
      row.className = 'promo-timeline-row' + (page.active ? '' : ' promo-timeline-row-inactive');

      const label = document.createElement('div');
      label.className = 'promo-timeline-row-label';
      label.textContent = `Page #${page.id} · 1/${page.frequency}`;
      label.title = label.textContent + (page.active ? '' : ' (inactive)');
      row.appendChild(label);

      const track = document.createElement('div');
      track.className = 'promo-timeline-track';
      track.dataset.pageId = String(page.id);

      const pageSchedules = cfg.schedules.filter((s) => s.page_id === page.id);
      pageSchedules.forEach((sch) => {
        segmentPieces(sch).forEach((piece) => {
          const seg = document.createElement('div');
          seg.className = 'promo-timeline-segment' + (piece.wrap ? ' is-wrap' : '');
          seg.style.left = (piece.left / DAY * 100) + '%';
          seg.style.width = Math.max(0, (piece.right - piece.left) / DAY * 100) + '%';
          seg.style.background = color;
          seg.dataset.scheduleId = String(sch.id);
          seg.title = `${toHHMM(sch.start_minutes)} → ${toHHMM(sch.end_minutes)} · ${segmentLabel(sch.frequency)}`
            + (piece.wrap ? ' (traverse minuit — modifiable via le formulaire)' : '');
          const labelSpan = document.createElement('span');
          labelSpan.textContent = segmentLabel(sch.frequency);
          seg.appendChild(labelSpan);

          if (!piece.wrap) {
            const left = document.createElement('div');
            left.className = 'promo-timeline-handle promo-timeline-handle-left';
            const right = document.createElement('div');
            right.className = 'promo-timeline-handle promo-timeline-handle-right';
            seg.appendChild(left);
            seg.appendChild(right);
            wireDrag(seg, left, right, track, sch);
          }

          track.appendChild(seg);
        });
      });

      const nowLine = document.createElement('div');
      nowLine.className = 'promo-timeline-now';
      nowLine.style.left = (nowMinutes / DAY * 100) + '%';
      track.appendChild(nowLine);

      row.appendChild(track);
      ROWS_EL.appendChild(row);
    });
  }

  // Glisser une poignée (redimensionne un bord) ou le corps du bloc (déplace
  // tout le créneau en gardant sa durée) — Pointer Events unifie souris et
  // tactile (écran de contrôle admin éventuellement tactile).
  function wireDrag(seg, leftHandle, rightHandle, track, sch) {
    function startDrag(mode) {
      return (e) => {
        e.preventDefault();
        e.stopPropagation();
        const trackRect = track.getBoundingClientRect();
        const minutesPerPixel = DAY / trackRect.width;
        const startX = e.clientX;
        const origStart = sch.start_minutes;
        const origEnd = sch.end_minutes;
        const duration = origEnd - origStart;
        seg.classList.add('is-dragging');
        seg.setPointerCapture && e.pointerId != null && seg.setPointerCapture(e.pointerId);

        function snap(m) { return Math.round(m / SNAP) * SNAP; }

        function onMove(ev) {
          const deltaMinutes = (ev.clientX - startX) * minutesPerPixel;
          let newStart = origStart, newEnd = origEnd;
          if (mode === 'move') {
            newStart = snap(origStart + deltaMinutes);
            newStart = Math.max(0, Math.min(DAY - duration, newStart));
            newEnd = newStart + duration;
          } else if (mode === 'left') {
            newStart = snap(origStart + deltaMinutes);
            newStart = Math.max(0, Math.min(origEnd - MIN_DURATION, newStart));
          } else if (mode === 'right') {
            newEnd = snap(origEnd + deltaMinutes);
            newEnd = Math.max(origStart + MIN_DURATION, Math.min(DAY, newEnd));
          }
          sch.start_minutes = newStart;
          sch.end_minutes = newEnd;
          seg.style.left = (newStart / DAY * 100) + '%';
          seg.style.width = Math.max(0, (newEnd - newStart) / DAY * 100) + '%';
          seg.title = `${toHHMM(newStart)} → ${toHHMM(newEnd)} · ${segmentLabel(sch.frequency)}`;
        }

        function onUp() {
          document.removeEventListener('pointermove', onMove);
          document.removeEventListener('pointerup', onUp);
          seg.classList.remove('is-dragging');
          if (sch.start_minutes !== origStart || sch.end_minutes !== origEnd) {
            syncFormRow(sch);
            persistSchedule(sch);
          }
        }

        document.addEventListener('pointermove', onMove);
        document.addEventListener('pointerup', onUp);
      };
    }

    leftHandle.addEventListener('pointerdown', startDrag('left'));
    rightHandle.addEventListener('pointerdown', startDrag('right'));
    seg.addEventListener('pointerdown', (e) => {
      // Ignore si le pointeur est sur une poignée (déjà gérée ci-dessus).
      if (e.target === leftHandle || e.target === rightHandle) return;
      startDrag('move')(e);
    });
  }

  function refreshNowLine() {
    const nowMinutes = (() => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); })();
    document.querySelectorAll('.promo-timeline-now').forEach((el) => {
      el.style.left = (nowMinutes / DAY * 100) + '%';
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    buildRuler();
    renderRows();
    setInterval(refreshNowLine, 60000);
  });
})();
