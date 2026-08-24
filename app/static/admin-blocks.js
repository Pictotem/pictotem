// Chrome partagé des blocs admin repliables / réorganisables — voir
// static/admin-blocks.css et app.py (_ADMIN_BLOCKS, /admin/ui/block_collapse,
// /admin/ui/block_order). Le déplacement d'un bloc vers une autre tuile
// (voir templates/blocks/*.html : formulaire .block-move-form) est une
// soumission de formulaire classique avec rechargement de page — pas de
// glisser-déposer en direct entre pages, volontairement, pour éviter tout
// conflit de script/identifiant entre deux blocs qui n'ont jamais été
// conçus pour coexister sur la même page (voir commentaire de
// admin_move_block dans app.py).
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

  document.addEventListener('DOMContentLoaded', () => {
    const container = document.querySelector('.admin-wrap');
    if (!container) return;
    const blocks = Array.from(container.querySelectorAll(':scope > [data-block-id]'));
    if (!blocks.length) return;
    const page = document.body.dataset.adminPage || '';

    // ── Replier / déplier ──────────────────────────────────────────────
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

    // ── Glisser-déposer pour réordonner dans la page (jamais entre pages) ─
    if (!page || blocks.length < 2) return;
    let dragged = null;

    function currentOrder() {
      return Array.from(container.querySelectorAll(':scope > [data-block-id]')).map((b) => b.dataset.blockId);
    }

    function clearDragOver() {
      container.querySelectorAll('[data-block-id].drag-over').forEach((b) => b.classList.remove('drag-over'));
    }

    blocks.forEach((block) => {
      const handle = block.querySelector(':scope > .block-header-row > .block-drag-handle');
      if (!handle) return;
      block.draggable = false;
      handle.addEventListener('mousedown', () => { block.draggable = true; });
      handle.addEventListener('mouseup', () => { block.draggable = false; });

      block.addEventListener('dragstart', (e) => {
        dragged = block;
        block.classList.add('dragging');
        if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
      });
      block.addEventListener('dragend', () => {
        block.draggable = false;
        block.classList.remove('dragging');
        clearDragOver();
      });
      block.addEventListener('dragover', (e) => {
        if (!dragged || dragged === block) return;
        e.preventDefault();
        block.classList.add('drag-over');
      });
      block.addEventListener('dragleave', () => block.classList.remove('drag-over'));
      block.addEventListener('drop', (e) => {
        if (!dragged || dragged === block) return;
        e.preventDefault();
        block.classList.remove('drag-over');
        const rect = block.getBoundingClientRect();
        const before = (e.clientY - rect.top) < rect.height / 2;
        block.parentNode.insertBefore(dragged, before ? block : block.nextSibling);
        postForm('/admin/ui/block_order', {
          _csrf_token: csrfToken(),
          page,
          order: currentOrder().join(','),
        });
        dragged = null;
      });
    });
  });
})();
