// Éditeur WYSIWYG des pages promo (voir templates/admin_slideshow.html →
// section "Pages promo") — construit sur Quill (vendorisé en local, voir
// static/quill.js/quill.snow.css : PAS de CDN, l'app tourne en kiosque,
// potentiellement sans accès internet). Une instance Quill par page promo,
// toolbar déclarative en HTML (voir admin_slideshow.html), reliée au champ
// caché html_content à l'envoi du formulaire.
//
// Deux choix volontaires plutôt que de tout déléguer à des modules tiers :
//  - Alignement du texte : attributor Quill natif (voir AlignStyle
//    ci-dessous), mais en variante "style" (text-align inline) plutôt que
//    "class" (défaut Quill) — la variante par défaut poserait class="ql-align-*"
//    sur les <p>, une classe CSS que sanitize_promo_html (utils.py) ne peut
//    pas valider aussi simplement qu'une déclaration de style unique. Même
//    résultat visuel, format de sortie mieux aligné avec le nettoyeur serveur
//    déjà en place.
//  - Redimensionnement + alignement d'image : PAS d'extension tierce
//    (quill-image-resize-module et consorts ne sont plus maintenus/pas
//    forcément compatibles Quill 2, et devraient de toute façon être
//    empaquetés nous-mêmes pour rester utilisables hors-ligne). On réutilise
//    à la place le code déjà écrit et testé pour l'ancien éditeur (poignée
//    de redimensionnement + barre flottante d'alignement), simplement relié
//    à Quill via un blot Image personnalisé (StyledImage, voir plus bas) qui
//    apprend à Quill à conserver l'attribut style="…" d'une image (Quill ne
//    connaît nativement que alt/width/height, jamais style) -- sans ça, le
//    redimensionnement/alignement survivrait à la session en cours mais
//    disparaîtrait au rechargement de la page.
(function () {
  if (typeof Quill === 'undefined') return;

  // ── Personnalisation Quill (une seule fois, avant toute instanciation) ──
  const AlignStyle = Quill.import('attributors/style/align');
  Quill.register(AlignStyle, true);

  const ImageFormat = Quill.import('formats/image');
  class StyledImage extends ImageFormat {
    static formats(domNode) {
      const formats = super.formats(domNode) || {};
      if (domNode.hasAttribute('style')) formats.style = domNode.getAttribute('style');
      return formats;
    }
    format(name, value) {
      if (name === 'style') {
        if (value) this.domNode.setAttribute('style', value);
        else this.domNode.removeAttribute('style');
      } else {
        super.format(name, value);
      }
    }
  }
  Quill.register(StyledImage, true);

  // ── Une instance Quill par page promo ───────────────────────────────────
  // Map id de conteneur ("wysiwyg-<page.id>") -> instance Quill, pour
  // retrouver la bonne instance à l'envoi du formulaire (voir tout en bas).
  const quillInstances = new Map();

  document.querySelectorAll('.promo-quill-editor').forEach(function (editorEl) {
    const toolbarEl = document.getElementById('toolbar-' + editorEl.id.replace(/^wysiwyg-/, ''));
    if (!toolbarEl) return;
    const quill = new Quill(editorEl, {
      theme: 'snow',
      // Quill lit son placeholder via l'option JS ci-dessous (jamais un
      // attribut HTML lu automatiquement) -- recopié depuis data-placeholder
      // (voir admin_slideshow.html) pour garder le texte au même endroit
      // (le template), plutôt que de le coder en dur ici.
      placeholder: editorEl.getAttribute('data-placeholder') || '',
      modules: {
        toolbar: {
          container: toolbarEl,
          handlers: {
            // Remplace le comportement par défaut du bouton image de Quill
            // (upload de fichier local en base64) par notre sélecteur
            // médiathèque/captures (voir plus bas).
            image: function () { openMediaPicker(quill); },
          },
        },
        table: true,
      },
    });
    quillInstances.set(editorEl.id, quill);

    const tableBtn = toolbarEl.querySelector('.promo-ql-table-btn');
    if (tableBtn) {
      tableBtn.addEventListener('click', function () {
        const range = quill.getSelection(true);
        quill.setSelection(range.index, 0, 'user');
        quill.getModule('table').insertTable(3, 3);
      });
    }

    const qrBtn = toolbarEl.querySelector('.promo-ql-qr-btn');
    if (qrBtn) {
      qrBtn.addEventListener('click', function () {
        const range = quill.getSelection(true);
        quill.insertText(range.index,
          '{qrcode="" taille="150" color="#000000" bgcolor="#ffffff"}', 'user');
      });
    }
  });

  // ── Sélecteur d'image (médiathèque + captures) ──────────────────────────
  // Un seul modal partagé par toutes les pages promo (voir
  // admin_slideshow.html → #media-picker-modal). La sélection Quill active
  // est perdue dès qu'on clique dans le modal : on la sauvegarde (Range
  // Quill {index, length}) à l'ouverture et on l'utilise à l'insertion.
  let _mediaPickerQuill = null;
  let _mediaPickerRange = null;

  function openMediaPicker(quill) {
    _mediaPickerQuill = quill;
    _mediaPickerRange = quill.getSelection(true);
    const modal = document.getElementById('media-picker-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
  }

  document.addEventListener('click', function (e) {
    const modal = document.getElementById('media-picker-modal');
    if (!modal || modal.classList.contains('hidden')) return;

    if (e.target === modal || e.target.closest('.media-picker-close')) {
      modal.classList.add('hidden');
      modal.setAttribute('aria-hidden', 'true');
      return;
    }

    const tab = e.target.closest('.media-picker-tab');
    if (tab) {
      modal.querySelectorAll('.media-picker-tab').forEach(function (t) {
        t.classList.toggle('active', t === tab);
      });
      const wanted = tab.getAttribute('data-tab');
      modal.querySelectorAll('.media-picker-grid').forEach(function (g) {
        g.classList.toggle('hidden', g.getAttribute('data-panel') !== wanted);
      });
      return;
    }

    const item = e.target.closest('.media-picker-item');
    if (item && _mediaPickerQuill) {
      const url = item.getAttribute('data-url') || '';
      const quill = _mediaPickerQuill;
      const range = _mediaPickerRange || quill.getSelection(true);
      quill.insertEmbed(range.index, 'image', url, 'user');
      quill.setSelection(range.index + 1, 0, 'user');
      // Largeur par défaut à l'insertion : sans elle, une capture en pleine
      // résolution s'afficherait à sa taille native (souvent énorme) avant
      // que l'admin ait pu la redimensionner. Reste ensuite ajustable par
      // glisser sur le coin (voir .wysiwyg-img-selected) ou par les boutons
      // d'alignement (voir la barre #wysiwyg-img-toolbar ci-dessous).
      // getLeaf(range.index) résoudrait au bloc PRÉCÉDENT quand l'image suit
      // immédiatement du texte (limite entre deux blots) -- range.index + 1
      // (une position à l'intérieur de l'unique emplacement qu'occupe
      // l'image) résout correctement dans tous les cas, vérifié y compris
      // quand l'image est en tout début de zone (rien avant elle).
      const [leaf] = quill.getLeaf(range.index + 1);
      if (leaf && leaf.domNode && leaf.domNode.tagName === 'IMG') {
        leaf.domNode.style.width = '240px';
      }
      modal.classList.add('hidden');
      modal.setAttribute('aria-hidden', 'true');
    }
  });

  // ── Redimensionnement + alignement d'une image du WYSIWYG ───────────────
  // Cliquer une image la sélectionne : poignée de redimensionnement MAISON
  // (le CSS `resize` natif sur <img> s'est révélé trop peu fiable selon les
  // navigateurs — poignée invisible), barre flottante d'alignement, et deux
  // champs Largeur/Hauteur (px) appliqués immédiatement à la frappe. Mutation
  // DIRECTE du style de l'image (pas d'appel à quill.format) -- testé et
  // confirmé fiable avec le blot StyledImage ci-dessus : Quill ne revient
  // jamais dessus, ni pendant la session, ni en relecture après enregistrement.
  let _selectedImg = null;

  function _positionImgToolbar(img) {
    const bar = document.getElementById('wysiwyg-img-toolbar');
    if (bar) {
      const r = img.getBoundingClientRect();
      bar.style.top = Math.max(8, r.top - 40) + 'px';
      bar.style.left = Math.max(8, r.left) + 'px';
      bar.classList.remove('hidden');
    }
    _positionResizeHandle(img);
  }

  function _positionResizeHandle(img) {
    const handle = document.getElementById('wysiwyg-img-resize-handle');
    if (!handle) return;
    const r = img.getBoundingClientRect();
    handle.style.top = Math.round(r.bottom - 7) + 'px';
    handle.style.left = Math.round(r.right - 7) + 'px';
    handle.classList.remove('hidden');
  }

  // Recopie la taille actuelle de l'image dans les champs Largeur/Hauteur —
  // appelé à la sélection et après un glisser, JAMAIS pendant que l'admin
  // tape dans ces champs (on écraserait sa frappe en cours).
  function _syncImgDimFields(img) {
    const r = img.getBoundingClientRect();
    const wInput = document.getElementById('wysiwyg-img-width');
    const hInput = document.getElementById('wysiwyg-img-height');
    if (wInput) wInput.value = Math.round(r.width);
    if (hInput) hInput.value = (img.style.height && img.style.height !== 'auto')
      ? Math.round(r.height) : '';
  }

  function _deselectImg() {
    if (_selectedImg) _selectedImg.classList.remove('wysiwyg-img-selected');
    const bar = document.getElementById('wysiwyg-img-toolbar');
    if (bar) bar.classList.add('hidden');
    const handle = document.getElementById('wysiwyg-img-resize-handle');
    if (handle) handle.classList.add('hidden');
    _selectedImg = null;
  }

  document.addEventListener('mousedown', function (e) {
    const img = e.target.closest('.promo-quill-editor .ql-editor img');
    const onControls = e.target.closest('#wysiwyg-img-toolbar, #wysiwyg-img-resize-handle');
    if (img) {
      if (_selectedImg && _selectedImg !== img) _deselectImg();
      _selectedImg = img;
      img.classList.add('wysiwyg-img-selected');
      _positionImgToolbar(img);
      _syncImgDimFields(img);
    } else if (!onControls && _selectedImg) {
      _deselectImg();
    }
  });

  // Glisser la poignée : ajuste la largeur en direct (hauteur recalculée en
  // "auto" pour ne jamais déformer l'image) — écouteurs mousemove/mouseup
  // posés seulement pendant le glisser, retirés ensuite.
  document.addEventListener('mousedown', function (e) {
    if (!e.target.closest('#wysiwyg-img-resize-handle') || !_selectedImg) return;
    e.preventDefault();
    const img = _selectedImg;
    const startX = e.clientX;
    const startWidth = img.getBoundingClientRect().width;

    function onMove(ev) {
      const w = Math.min(2000, Math.max(20, Math.round(startWidth + (ev.clientX - startX))));
      img.style.width = w + 'px';
      img.style.height = 'auto';
      _positionImgToolbar(img);
      _syncImgDimFields(img);
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // Champs Largeur/Hauteur : prise en compte immédiate à chaque frappe.
  // Largeur seule saisie => hauteur recalculée "auto" (proportionnelle) ;
  // dès que Hauteur est renseignée, elle devient une valeur fixe indépendante.
  document.addEventListener('input', function (e) {
    if (!_selectedImg) return;
    if (e.target.id === 'wysiwyg-img-width') {
      const v = parseInt(e.target.value, 10);
      if (v > 0) {
        _selectedImg.style.width = Math.min(2000, Math.max(20, v)) + 'px';
        const hInput = document.getElementById('wysiwyg-img-height');
        if (!hInput || !hInput.value) _selectedImg.style.height = 'auto';
        _positionImgToolbar(_selectedImg);
      }
    } else if (e.target.id === 'wysiwyg-img-height') {
      const v = parseInt(e.target.value, 10);
      if (v > 0) {
        _selectedImg.style.height = Math.min(2000, Math.max(20, v)) + 'px';
        _positionImgToolbar(_selectedImg);
      } else if (e.target.value === '') {
        _selectedImg.style.height = 'auto';
        _positionImgToolbar(_selectedImg);
      }
    }
  });

  document.addEventListener('click', function (e) {
    if (!_selectedImg) return;

    const delBtn = e.target.closest('#wysiwyg-img-toolbar button[data-action="delete"]');
    if (delBtn) {
      e.preventDefault();
      const img = _selectedImg;
      _selectedImg = null;
      img.remove();
      const bar = document.getElementById('wysiwyg-img-toolbar');
      if (bar) bar.classList.add('hidden');
      const handle = document.getElementById('wysiwyg-img-resize-handle');
      if (handle) handle.classList.add('hidden');
      return;
    }

    const alignBtn = e.target.closest('#wysiwyg-img-toolbar button[data-align]');
    if (!alignBtn) return;
    e.preventDefault();
    const align = alignBtn.getAttribute('data-align');
    const img = _selectedImg;
    img.style.float = '';
    img.style.display = '';
    img.style.marginTop = '';
    img.style.marginRight = '';
    img.style.marginBottom = '';
    img.style.marginLeft = '';
    if (align === 'left') {
      img.style.float = 'left';
      img.style.marginTop = '0';
      img.style.marginRight = '16px';
      img.style.marginBottom = '12px';
      img.style.marginLeft = '0';
    } else if (align === 'right') {
      img.style.float = 'right';
      img.style.marginTop = '0';
      img.style.marginRight = '0';
      img.style.marginBottom = '12px';
      img.style.marginLeft = '16px';
    } else if (align === 'center') {
      img.style.display = 'block';
      img.style.marginTop = '0';
      img.style.marginRight = 'auto';
      img.style.marginBottom = '12px';
      img.style.marginLeft = 'auto';
    }
    // align === 'inline' : tout déjà remis à vide ci-dessus.
    _positionImgToolbar(img);
  });

  window.addEventListener('scroll', function () {
    if (_selectedImg) _positionImgToolbar(_selectedImg);
  }, true);
  window.addEventListener('resize', function () {
    if (_selectedImg) _positionImgToolbar(_selectedImg);
  });

  // Recopie le HTML de l'instance Quill de la page dans son champ caché
  // juste avant l'envoi du formulaire (capture=true : passe avant toute
  // autre écoute 'submit', pour être sûr que le champ est à jour même si le
  // navigateur déclenche la validation native entre-temps).
  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!form.classList || !form.classList.contains('promo-page-form')) return;
    // Désélectionne une image avant de lire le HTML — sinon la classe
    // wysiwyg-img-selected (purement visuelle, jamais censée être
    // enregistrée) partirait dans le contenu (elle serait de toute façon
    // retirée par le nettoyeur serveur, mais autant rester propre).
    if (_selectedImg && form.contains(_selectedImg)) _deselectImg();
    const editorEl = form.querySelector('.promo-quill-editor');
    const hidden = form.querySelector('input[type=hidden][name=html_content]');
    const quill = editorEl && quillInstances.get(editorEl.id);
    if (quill && hidden) hidden.value = quill.root.innerHTML;
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
