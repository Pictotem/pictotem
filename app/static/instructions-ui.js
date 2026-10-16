// Helpers partagés (kiosque + aperçu admin) pour la page d'instructions et le
// bouton « Aide » : positionnement sur une grille de 9 points avec marges,
// et mise en forme des boutons. Garde-fous volontaires : cible tactile
// minimale et marges bornées à la fenêtre, pour qu'un réglage extrême ne
// rende jamais le bouton de fermeture introuvable.
(function () {
  const MIN_BTN = 56;

  function clampMargin(m, size, total) {
    const max = Math.max(0, total - size - 8);
    return Math.max(0, Math.min(Number(m) || 0, max));
  }

  // position : 'top-left' … 'bottom-right' (vertical-horizontal).
  function applyPosition(el, position, mx, my, size) {
    const parts = String(position || 'top-right').split('-');
    const v = parts[0], h = parts[1];
    const s = el.style;
    s.top = s.bottom = s.left = s.right = s.transform = '';
    const x = clampMargin(mx, size, window.innerWidth);
    const y = clampMargin(my, size, window.innerHeight);
    let tx = '', ty = '';
    if (v === 'top') s.top = y + 'px';
    else if (v === 'bottom') s.bottom = y + 'px';
    else { s.top = '50%'; ty = 'translateY(-50%)'; }
    if (h === 'left') s.left = x + 'px';
    else if (h === 'right') s.right = x + 'px';
    else { s.left = '50%'; tx = 'translateX(-50%)'; }
    s.transform = [tx, ty].filter(Boolean).join(' ');
  }

  function styleCloseButton(el, c) {
    const size = Math.max(MIN_BTN, Number(c.size) || 72);
    const isSymbol = c.kind !== 'text';
    el.textContent = c.label || '✕';
    el.style.position = 'absolute';
    el.style.background = c.bg;
    el.style.color = c.fg;
    el.style.height = size + 'px';
    el.style.minWidth = size + 'px';
    el.style.width = isSymbol ? size + 'px' : 'auto';
    el.style.padding = isSymbol ? '0' : '0 ' + Math.round(size * 0.4) + 'px';
    el.style.borderRadius = isSymbol ? '50%' : Math.round(size / 2) + 'px';
    el.style.fontSize = Math.round(size * (isSymbol ? 0.5 : 0.38)) + 'px';
    applyPosition(el, c.position, c.margin_x, c.margin_y, size);
  }

  function styleHelpButton(el, h, floating) {
    const size = Math.max(MIN_BTN, Number(h.size) || 72);
    const round = h.shape === 'circle';
    el.textContent = h.label || 'Aide';
    el.style.background = h.bg;
    el.style.color = h.fg;
    el.style.height = size + 'px';
    el.style.minWidth = size + 'px';
    el.style.width = round ? size + 'px' : 'auto';
    el.style.padding = round ? '0' : '0 ' + Math.round(size * 0.35) + 'px';
    el.style.borderRadius = h.radius || '999px';
    el.style.fontSize = Math.round(size * (round ? 0.3 : 0.38)) + 'px';
    if (floating) {
      el.style.position = 'fixed';
      applyPosition(el, h.position, h.margin_x, h.margin_y, size);
    } else {
      el.style.position = '';
      el.style.top = el.style.bottom = el.style.left = el.style.right = el.style.transform = '';
    }
  }

  window.PictotemInstructionsUI = { applyPosition, styleCloseButton, styleHelpButton, MIN_BTN };
})();
