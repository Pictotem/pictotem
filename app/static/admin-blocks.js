// Chrome partagé des blocs admin repliables / réorganisables — voir
// static/admin-blocks.css et app.py (_ADMIN_BLOCKS, /admin/ui/block_collapse,
// /admin/ui/block_order). Le déplacement d'un bloc vers une autre tuile
// (voir templates/blocks/*.html : formulaire .block-move-form) est une
// soumission de formulaire classique avec rechargement de page — pas de
// glisser-déposer en direct entre pages, volontairement, pour éviter tout
// conflit de script/identifiant entre deux blocs qui n'ont jamais été
// conçus pour coexister sur la même page (voir commentaire de
// admin_move_block dans app.py).
//
// Depuis la vague 11 (CRUD des tuiles), ce fichier réordonne aussi les
// CARTES de tuiles sur /admin (voir templates/admin_home.html, conteneur
// .tuiles-grid, attribut data-tuile-id) — même mécanique de glisser-déposer
// que les blocs, généralisée dans wireDragReorder ci-dessous plutôt que
// dupliquée : un seul réordonnancement à la fois (une page à blocs n'a
// jamais de .tuiles-grid, et /admin n'a jamais de [data-block-id]), donc
// aucun risque de conflit entre les deux usages.
(function () {
  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  function postForm(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(data),
    }).catch(() => {});
  }

  // Glisser-déposer natif HTML5 pour réordonner les enfants directs de
  // `container` marqués `[data-${idAttr}]`, via leur poignée
  // `handleSelector` (relative à chaque enfant) — poste le nouvel ordre à
  // `postUrl` avec `extraFields` fusionnés. Utilisé pour les blocs
  // (data-block-id, poignée dans .block-header-row, /admin/ui/block_order)
  // ET pour les tuiles (data-tuile-id, poignée dans .tuile-card-head,
  // /admin/ui/tuile_order).
  function wireDragReorder(container, idAttr, handleSelector, postUrl, extraFields) {
    const items = Array.from(container.querySelectorAll(`:scope > [data-${idAttr}]`));
    if (items.length < 2) return;
    let dragged = null;

    function currentOrder() {
      return Array.from(container.querySelectorAll(`:scope > [data-${idAttr}]`))
        .map((el) => el.dataset[idAttr.replace(/-([a-z])/g, (_, c) => c.toUpperCase())]);
    }

    function clearDragOver() {
      container.querySelectorAll(`[data-${idAttr}].drag-over`).forEach((el) => el.classList.remove('drag-over'));
    }

    items.forEach((item) => {
      const handle = item.querySelector(handleSelector);
      if (!handle) return;
      item.draggable = false;
      handle.addEventListener('mousedown', () => { item.draggable = true; });
      handle.addEventListener('mouseup', () => { item.draggable = false; });

      item.addEventListener('dragstart', (e) => {
        dragged = item;
        item.classList.add('dragging');
        if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
      });
      item.addEventListener('dragend', () => {
        item.draggable = false;
        item.classList.remove('dragging');
        clearDragOver();
      });
      item.addEventListener('dragover', (e) => {
        if (!dragged || dragged === item) return;
        e.preventDefault();
        item.classList.add('drag-over');
      });
      item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
      item.addEventListener('drop', (e) => {
        if (!dragged || dragged === item) return;
        e.preventDefault();
        item.classList.remove('drag-over');
        const rect = item.getBoundingClientRect();
        const before = (e.clientY - rect.top) < rect.height / 2;
        item.parentNode.insertBefore(dragged, before ? item : item.nextSibling);
        postForm(postUrl, { _csrf_token: csrfToken(), order: currentOrder().join(','), ...extraFields });
        dragged = null;
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    // ── Blocs (pages à blocs : /admin/application, /admin/guest_codes...) ─
    const container = document.querySelector('.admin-wrap');
    if (container) {
      const blocks = Array.from(container.querySelectorAll(':scope > [data-block-id]'));
      if (blocks.length) {
        const page = document.body.dataset.adminPage || '';

        // Replier / déplier
        blocks.forEach((block) => {
          const toggle = block.querySelector(':scope > .block-header-row > .block-collapse-toggle');
          if (!toggle) return;
          toggle.addEventListener('click', () => {
            const collapsed = block.classList.toggle('block-collapsed');
            postForm('/admin/ui/block_collapse', {
              _csrf_token: csrfToken(),
              block_id: block.dataset.blockId,
              collapsed: collapsed ? '1' : '0',
            });
          });
        });

        // Glisser-déposer pour réordonner DANS la page (jamais entre pages)
        if (page) {
          wireDragReorder(container, 'block-id', ':scope > .block-header-row > .block-drag-handle',
            '/admin/ui/block_order', { page });
        }
      }
    }

    // ── Tuiles (grille réordonnable de /admin — voir admin_home.html) ─────
    const tuilesGrid = document.querySelector('.tuiles-grid');
    if (tuilesGrid) {
      wireDragReorder(tuilesGrid, 'tuile-id', ':scope > .tuile-card-head > .block-drag-handle',
        '/admin/ui/tuile_order', {});
    }
  });
})();
