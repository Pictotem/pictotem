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

  // v2.0.4 : police / taille de texte (boutons WYSIWYG, remplacent les
  // anciens champs indépendants "Police"/"Taille du texte") -- les
  // attributors 'formats/font' et 'formats/size' par défaut de Quill sont
  // des variantes CLASS avec liste blanche (ql-font-serif/-monospace,
  // ql-size-small/-large/-huge uniquement) : on les remplace ici par des
  // StyleAttributor tout neufs, SANS liste blanche (contrairement à
  // `Quill.import('attributors/style/font'|'style/size')`, qui reprend la
  // même liste blanche que la variante class) -- même principe que
  // AlignStyle ci-dessus, qui lui n'avait pas ce problème (l'attributor
  // style d'alignement n'a jamais eu de liste blanche). Résultat : n'importe
  // quelle police (y compris les polices personnalisées, voir _all_fonts()
  // côté serveur) et n'importe quelle taille en px restent utilisables,
  // aussi bien depuis les <select> de la barre d'outils que depuis le mode
  // source (voir la modale de tableau plus bas pour le même principe
  // appliqué aux styles de tableau).
  const Parchment = Quill.import('parchment');
  const FontStyle = new Parchment.StyleAttributor('font', 'font-family', { scope: Parchment.Scope.INLINE });
  Quill.register(FontStyle, true);
  const SizeStyle = new Parchment.StyleAttributor('size', 'font-size', { scope: Parchment.Scope.INLINE });
  Quill.register(SizeStyle, true);

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

    // v2.0.4 : le bouton tableau ouvre désormais la modale « Propriétés du
    // tableau » (voir #table-props-modal, admin_slideshow.html, et la
    // section dédiée plus bas) -- insertion 3×3 immédiate ou édition d'un
    // tableau existant selon la position du curseur, plutôt qu'un raccourci
    // figé à 3×3.
    const tableBtn = toolbarEl.querySelector('.promo-ql-table-btn');
    if (tableBtn) {
      tableBtn.addEventListener('click', function () { openTableProps(quill); });
    }

    const qrBtn = toolbarEl.querySelector('.promo-ql-qr-btn');
    if (qrBtn) {
      qrBtn.addEventListener('click', function () {
        const range = quill.getSelection(true);
        quill.insertText(range.index,
          '{qrcode="" taille="150" color="#000000" bgcolor="#ffffff"}', 'user');
      });
    }

    // ── Couleur du texte (input color natif -- voir commentaire plus haut
    // sur FontStyle/SizeStyle : contrairement au select ql-align déjà géré
    // nativement par Quill, un <input type=color> donne accès à toute la
    // palette plutôt qu'à une poignée de nuances prédéfinies, cohérent avec
    // les autres couleurs de ce formulaire (fonds, dégradés...), toutes des
    // <input type=color>). La sélection Quill active est sauvegardée au
    // mousedown (avant que le natif ne vole le focus pour son propre
    // sélecteur de couleur) -- même principe que _mediaPickerRange plus
    // bas pour le sélecteur d'image.
    const colorInput = toolbarEl.querySelector('.promo-ql-color-input');
    const colorClearBtn = toolbarEl.querySelector('.promo-ql-color-clear');
    let colorRange = null;
    if (colorInput) {
      colorInput.addEventListener('mousedown', function () {
        colorRange = quill.getSelection(true);
      });
      colorInput.addEventListener('input', function () {
        const range = colorRange || quill.getSelection(true);
        if (!range) return;
        if (range.length > 0) {
          quill.formatText(range.index, range.length, 'color', colorInput.value, 'user');
        } else {
          quill.setSelection(range.index, 0, 'silent');
          quill.format('color', colorInput.value, 'user');
        }
      });
    }
    if (colorClearBtn) {
      colorClearBtn.addEventListener('click', function () {
        const range = quill.getSelection(true);
        if (!range) return;
        if (range.length > 0) quill.formatText(range.index, range.length, 'color', false, 'user');
        else quill.format('color', false, 'user');
      });
    }

    // ── Mode source (voir #source-wrap-<id>, admin_slideshow.html) : bascule
    // entre l'éditeur visuel et un <textarea> affichant le HTML brut (balises
    // + styles en ligne), avec aperçu en direct dans un second panneau. Le
    // retour au mode visuel repasse par quill.clipboard.dangerouslyPasteHTML
    // (plutôt qu'une simple assignation à quill.root.innerHTML) pour que le
    // Delta interne de Quill reste cohérent avec le DOM affiché -- une
    // affectation directe laisserait Quill croire que son ancien contenu est
    // toujours d'actualité et provoquerait des incohérences à la prochaine
    // frappe.
    const pageSuffix = editorEl.id.replace(/^wysiwyg-/, '');
    const sourceBtn = toolbarEl.querySelector('.promo-ql-source-btn');
    const sourceWrap = document.getElementById('source-wrap-' + pageSuffix);
    const sourceCode = document.getElementById('source-code-' + pageSuffix);
    const sourcePreview = document.getElementById('source-preview-' + pageSuffix);
    if (sourceBtn && sourceWrap && sourceCode && sourcePreview) {
      sourceBtn.addEventListener('click', function () {
        const inSource = !sourceWrap.classList.contains('hidden');
        if (!inSource) {
          sourceCode.value = quill.root.innerHTML;
          sourcePreview.innerHTML = sourceCode.value;
          editorEl.classList.add('hidden');
          sourceWrap.classList.remove('hidden');
          toolbarEl.classList.add('promo-source-active');
        } else {
          quill.clipboard.dangerouslyPasteHTML(sourceCode.value, 'user');
          editorEl.classList.remove('hidden');
          sourceWrap.classList.add('hidden');
          toolbarEl.classList.remove('promo-source-active');
        }
      });
      sourceCode.addEventListener('input', function () {
        sourcePreview.innerHTML = sourceCode.value;
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
    if (!hidden) return;
    // Mode source actif (voir plus haut) : le <textarea> est la source de
    // vérité, on l'utilise directement plutôt que quill.root.innerHTML (qui
    // n'a plus été mis à jour depuis le passage en mode source).
    const pageSuffix = editorEl ? editorEl.id.replace(/^wysiwyg-/, '') : '';
    const sourceWrap = document.getElementById('source-wrap-' + pageSuffix);
    const sourceCode = document.getElementById('source-code-' + pageSuffix);
    if (sourceWrap && sourceCode && !sourceWrap.classList.contains('hidden')) {
      hidden.value = sourceCode.value;
      return;
    }
    const quill = editorEl && quillInstances.get(editorEl.id);
    if (quill) hidden.value = quill.root.innerHTML;
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

  // ── Modale « Propriétés du tableau » (voir #table-props-modal, template)
  // ────────────────────────────────────────────────────────────────────
  // Un seul jeu de contrôles partagé par toutes les pages promo (comme le
  // sélecteur média ci-dessus). Deux registres bien séparés :
  //  - structure (insérer/supprimer ligne/colonne/tableau) : passe TOUJOURS
  //    par this.quill.getModule('table') pour garder le Delta interne de
  //    Quill cohérent avec le DOM -- jamais de manipulation DOM directe ici ;
  //  - mise en forme (largeur/hauteur/bordure/marge/padding/couleur) :
  //    mutation DIRECTE du style des éléments <table>/<tr>/<td> concernés,
  //    comme pour le redimensionnement d'image plus haut -- pas de format
  //    Quill dédié pour ces réglages, mais innerHTML lu à l'envoi du
  //    formulaire (voir plus haut) capture bien le style ainsi posé.
  //
  // getTable() du module table (this.quill.getModule('table').getTable())
  // s'appuie sur la sélection Quill COURANTE (this.quill.getSelection(),
  // sans forcer le focus) -- avant chaque action structurelle, on rappelle
  // donc explicitement quill.getSelection(true) pour restaurer le focus et
  // la dernière sélection connue (perdue dès qu'on clique dans la modale,
  // hors de l'éditeur), exactement comme le fait déjà le bouton d'insertion
  // de tableau ou le sélecteur média ci-dessus.
  let _tpQuill = null;
  let _tpTableEl = null;
  let _tpRowEl = null;
  let _tpCellEl = null;
  let _tpColIndex = -1;

  function _tpModal() { return document.getElementById('table-props-modal'); }

  function _tpRefreshFromSelection(quill) {
    const table = quill.getModule('table');
    const [tableBlot, rowBlot, cellBlot] = table.getTable();
    if (!tableBlot || !rowBlot || !cellBlot) {
      _tpTableEl = null; _tpRowEl = null; _tpCellEl = null; _tpColIndex = -1;
      return false;
    }
    _tpTableEl = tableBlot.domNode;
    _tpRowEl = rowBlot.domNode;
    _tpCellEl = cellBlot.domNode;
    _tpColIndex = (typeof cellBlot.cellOffset === 'function') ? cellBlot.cellOffset() : -1;
    return true;
  }

  function openTableProps(quill) {
    _tpQuill = quill;
    const inTable = _tpRefreshFromSelection(quill);
    const modal = _tpModal();
    if (!modal) return;
    modal.setAttribute('data-mode', inTable ? 'edit' : 'insert');
    const wInput = document.getElementById('tp-col-width');
    const hInput = document.getElementById('tp-row-height');
    if (wInput) wInput.value = (inTable && _tpCellEl) ? Math.round(_tpCellEl.getBoundingClientRect().width) : '';
    if (hInput) hInput.value = (inTable && _tpRowEl) ? Math.round(_tpRowEl.getBoundingClientRect().height) : '';
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
  }

  function _tpClose() {
    const modal = _tpModal();
    if (!modal) return;
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
  }

  function _tpWithFreshSelection(fn) {
    if (!_tpQuill) return;
    _tpQuill.getSelection(true);
    fn(_tpQuill.getModule('table'));
    _tpRefreshFromSelection(_tpQuill);
  }

  function _tpApplyColumnWidth(px) {
    if (!_tpTableEl || _tpColIndex < 0 || !(px > 0)) return;
    _tpTableEl.querySelectorAll('tr').forEach(function (tr) {
      const cell = tr.children[_tpColIndex];
      if (cell) cell.style.width = px + 'px';
    });
  }

  function _tpApplyRowHeight(px) {
    if (!_tpRowEl || !(px > 0)) return;
    _tpRowEl.style.height = px + 'px';
  }

  function _tpApplyBorder(color, widthPx, style) {
    if (!_tpTableEl) return;
    _tpTableEl.querySelectorAll('td, th').forEach(function (cell) {
      if (style === 'none') {
        cell.style.borderStyle = 'none';
      } else {
        cell.style.borderWidth = Math.max(0, widthPx || 0) + 'px';
        cell.style.borderStyle = style || 'solid';
        cell.style.borderColor = color;
      }
    });
  }

  function _tpApplyPadding(px) {
    if (!_tpTableEl || !(px >= 0)) return;
    _tpTableEl.querySelectorAll('td, th').forEach(function (cell) {
      cell.style.padding = px + 'px';
    });
  }

  function _tpApplyMargin(px, centered) {
    if (!_tpTableEl || !(px >= 0)) return;
    _tpTableEl.style.margin = centered ? (px + 'px auto') : (px + 'px');
  }

  function _tpApplyBackground(scope, color) {
    if (!_tpTableEl) return;
    if (scope === 'cell' && _tpCellEl) {
      _tpCellEl.style.backgroundColor = color;
    } else if (scope === 'row' && _tpRowEl) {
      _tpRowEl.querySelectorAll('td, th').forEach(function (c) { c.style.backgroundColor = color; });
    } else if (scope === 'table') {
      _tpTableEl.querySelectorAll('td, th').forEach(function (c) { c.style.backgroundColor = color; });
    }
  }

  function _tpClearBackground() {
    if (!_tpTableEl) return;
    _tpTableEl.querySelectorAll('td, th').forEach(function (c) { c.style.backgroundColor = ''; });
  }

  document.addEventListener('click', function (e) {
    const modal = _tpModal();
    if (!modal || modal.classList.contains('hidden')) return;

    if (e.target === modal || e.target.closest('.table-props-close')) {
      _tpClose();
      return;
    }

    if (e.target.id === 'tp-insert-btn') {
      const cols = parseInt(document.getElementById('tp-insert-cols').value, 10) || 3;
      const rows = parseInt(document.getElementById('tp-insert-rows').value, 10) || 3;
      _tpWithFreshSelection(function (table) { table.insertTable(rows, cols); });
      _tpClose();
      return;
    }

    const action = e.target.closest('[data-tp-action]');
    if (!action) return;
    const act = action.getAttribute('data-tp-action');

    if (act === 'col-left') _tpWithFreshSelection(function (t) { t.insertColumnLeft(); });
    else if (act === 'col-right') _tpWithFreshSelection(function (t) { t.insertColumnRight(); });
    else if (act === 'col-delete') _tpWithFreshSelection(function (t) { t.deleteColumn(); });
    else if (act === 'row-above') _tpWithFreshSelection(function (t) { t.insertRowAbove(); });
    else if (act === 'row-below') _tpWithFreshSelection(function (t) { t.insertRowBelow(); });
    else if (act === 'row-delete') _tpWithFreshSelection(function (t) { t.deleteRow(); });
    else if (act === 'apply-col-width') _tpApplyColumnWidth(parseInt(document.getElementById('tp-col-width').value, 10));
    else if (act === 'apply-row-height') _tpApplyRowHeight(parseInt(document.getElementById('tp-row-height').value, 10));
    else if (act === 'apply-border') _tpApplyBorder(
        document.getElementById('tp-border-color').value,
        parseInt(document.getElementById('tp-border-width').value, 10),
        document.getElementById('tp-border-style').value);
    else if (act === 'apply-padding') _tpApplyPadding(parseInt(document.getElementById('tp-padding').value, 10));
    else if (act === 'apply-margin') _tpApplyMargin(
        parseInt(document.getElementById('tp-margin').value, 10) || 0,
        !!document.getElementById('tp-margin-center').checked);
    else if (act === 'apply-bg-cell') _tpApplyBackground('cell', document.getElementById('tp-bg-color').value);
    else if (act === 'apply-bg-row') _tpApplyBackground('row', document.getElementById('tp-bg-color').value);
    else if (act === 'apply-bg-table') _tpApplyBackground('table', document.getElementById('tp-bg-color').value);
    else if (act === 'clear-bg-table') _tpClearBackground();
    else if (act === 'delete-table') { _tpWithFreshSelection(function (t) { t.deleteTable(); }); _tpClose(); }
  });
})();
