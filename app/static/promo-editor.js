// Éditeur WYSIWYG minimal des pages promo (voir templates/admin_slideshow.html
// → section "Pages promo") : une barre d'outils par page agit sur sa propre
// zone contenteditable (data-target sur .wysiwyg-toolbar ↔ id sur
// .wysiwyg-editable), via document.execCommand — volontairement basique
// (gras/italique/souligné/listes/alignement), aucune dépendance externe, pas
// de CDN (l'app tourne en kiosque, potentiellement sans accès internet). Le
// HTML produit est nettoyé côté serveur avant stockage (voir
// utils.sanitize_promo_html) : seules ces mises en forme survivent de toute
// façon, tout le reste (recopié depuis une autre page par exemple) est
// silencieusement retiré à l'enregistrement.
(function () {
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('.wysiwyg-toolbar button[data-cmd]');
    if (!btn) return;
    e.preventDefault();
    const toolbar = btn.closest('.wysiwyg-toolbar');
    const editable = document.getElementById(toolbar.getAttribute('data-target'));
    if (!editable) return;
    editable.focus();
    document.execCommand(btn.getAttribute('data-cmd'), false, null);
  });

  // Recopie le HTML de la zone éditable dans le champ caché juste avant
  // l'envoi du formulaire (capture=true : passe avant toute autre écoute
  // 'submit', pour être sûr que le champ est à jour même si le navigateur
  // déclenche la validation native entre-temps).
  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!form.classList || !form.classList.contains('promo-page-form')) return;
    const editable = form.querySelector('.wysiwyg-editable');
    const hidden = form.querySelector('input[type=hidden][name=html_content]');
    if (editable && hidden) hidden.value = editable.innerHTML;
  }, true);

  // Met en évidence le fond actuellement sélectionné dans le sélecteur radio
  // (en plus du support CSS :has() natif — ceinture et bretelles si un
  // navigateur plus ancien venait à afficher le back office).
  document.addEventListener('change', function (e) {
    const input = e.target;
    if (input.name !== 'background_id') return;
    const group = input.closest('.promo-bg-picker');
    if (!group) return;
    group.querySelectorAll('.promo-bg-choice').forEach(function (label) {
      const radio = label.querySelector('input[name=background_id]');
      label.classList.toggle('selected', !!radio && radio.checked);
    });
  });

  // Déplier/replier une page promo (voir .promo-page-toggle-open côté
  // template) — état purement visuel, non persisté.
  document.addEventListener('click', function (e) {
    const toggle = e.target.closest('.promo-page-toggle-open');
    if (!toggle) return;
    const card = toggle.closest('.promo-page-card');
    if (card) card.classList.toggle('promo-page-collapsed');
  });
})();
