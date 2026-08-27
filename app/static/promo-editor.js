// Éditeur WYSIWYG des pages promo (voir templates/admin_slideshow.html →
// section "Pages promo") — construit sur CKEditor 5 (vendorisé en local,
// licence GPL auto-hébergée, voir static/ckeditor5/ : PAS de CDN, aucune
// clé API, aucun appel réseau — l'app tourne en kiosque, potentiellement
// sans accès internet).
//
// v2.0.9 : remplace l'éditeur Quill précédent. Motif du changement : Quill
// n'offre aucun module tableau fiable (voir l'historique de ce fichier
// avant cette version -- un blot "table opaque" fait à la main, avec
// contenteditable imbriqué, avait dû être construit pour contourner ce
// manque). CKEditor 5 gère tableaux ET images nativement (widgets avec
// poignées de redimensionnement, panneaux de propriétés, fusion de
// cellules...) -- tout le bricolage précédent (modale "Propriétés du
// tableau", poignée de redimensionnement d'image maison, blot PromoTable)
// disparaît, remplacé par la configuration de plugins ci-dessous.
//
// Ce qui reste spécifique à Pictotem (non couvert par CKEditor lui-même) :
//  - Le sélecteur d'image maison (médiathèque/captures/images dédiées, voir
//    #media-picker-modal) -- CKEditor n'a pas de médiathèque intégrée, un
//    plugin maison (PromoInsertImage ci-dessous) ouvre notre propre modal.
//  - L'insertion du texte-espaceur {qrcode=...} (résolu côté serveur à
//    l'affichage, voir _resolve_inline_qrcodes, app.py) -- un second
//    plugin maison (PromoInsertQr).
//  - La couleur de fond de la zone d'édition (aide visuelle admin
//    uniquement, voir editor_bg_color) -- appliquée après coup sur
//    l'élément éditable réel de CKEditor.
(function () {
  if (typeof CKEDITOR === 'undefined') return;

  const {
    ClassicEditor, Essentials, Paragraph, Bold, Italic, Underline,
    FontFamily, FontSize, FontColor, FontBackgroundColor, Alignment,
    List, Table, TableToolbar, TableProperties, TableCellProperties,
    TableColumnResize, Image, ImageToolbar, ImageStyle, ImageResize,
    ImageCaption, RemoveFormat, SourceEditing, Plugin, ButtonView,
    ModelLivePosition,
  } = CKEDITOR;

  // ── Config police/taille (voir #promo-editor-config, admin_slideshow.html)
  // -- listes alimentées côté serveur (_all_fonts(), promo_text_sizes),
  // comme les anciens <select> ql-font/ql-size, mais lues ici en JSON
  // plutôt qu'un <select> : CKEditor construit lui-même son propre menu
  // déroulant pour FontFamily/FontSize.
  let _cfg = { fonts: [], sizes: [28, 48] };
  const cfgEl = document.getElementById('promo-editor-config');
  if (cfgEl) {
    try { _cfg = JSON.parse(cfgEl.textContent); } catch (e) { /* défauts ci-dessus */ }
  }
  const fontFamilyOptions = ['default'].concat(
    _cfg.fonts.map(function (f) { return { title: f[1], model: f[0] }; })
  );
  const fontSizeOptions = ['default'].concat(_cfg.sizes);

  // ── Palette de couleurs de la charte graphique (voir /admin/charte, CRUD
  // illimité) -- alimente la palette prédéfinie du sélecteur de couleur
  // fontColor/fontBackgroundColor ci-dessous, en plus du sélecteur "pipette"
  // libre (colorPicker) déjà configuré. Vide (aucune couleur créée) : le
  // paramètre `colors` n'est pas transmis, CKEditor retombe alors sur sa
  // palette par défaut.
  const charteColorOptions = (_cfg.colors || []).map(function (c) {
    return { color: c[0], label: c[1] };
  });

  // ── Icônes des boutons maison (SVG minimal, cohérent avec le jeu
  // d'icônes CKEditor -- viewBox 0 0 20 20, trait plein). ─────────────────
  const ICON_IMAGE = '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M2 4a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4zm2 1v9.6L8.5 10l3 3 3.5-4L16 11.5V5H4zM7 8a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>';
  const ICON_QR = '<svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path d="M3 3h5v5H3V3zm1.5 1.5v2h2v-2h-2zM3 12h5v5H3v-5zm1.5 1.5v2h2v-2h-2zM12 3h5v5h-5V3zm1.5 1.5v2h2v-2h-2zM12 12h2v2h-2v-2zm3 0h2v2h-2v-2zm-3 3h2v2h-2v-2zm3 0h2v2h-2v-2z"/></svg>';

  // ── Plugin maison : bouton "Insérer une image" (médiathèque/captures/
  // images dédiées, voir #media-picker-modal ci-dessous) -- remplace le
  // bouton image natif de CKEditor (upload de fichier local), jamais
  // pertinent ici (l'admin choisit toujours parmi des images déjà sur le
  // serveur, jamais un fichier de son poste). ─────────────────────────────
  class PromoInsertImage extends Plugin {
    init() {
      const editor = this.editor;
      editor.ui.componentFactory.add('promoInsertImage', function (locale) {
        const view = new ButtonView(locale);
        view.set({
          label: 'Insérer une image (médiathèque, captures, images dédiées)',
          icon: ICON_IMAGE,
          tooltip: true,
        });
        view.on('execute', function () { openMediaPicker(editor); });
        return view;
      });
    }
  }

  // ── Plugin maison : bouton "Insérer un QR code" -- insère le texte-
  // espaceur {qrcode="" ...}, résolu en image QR côté serveur à
  // l'affichage (voir _resolve_inline_qrcodes, app.py) : jamais un vrai
  // <img> dans l'éditeur, juste du texte comme n'importe quel autre mot. */
  class PromoInsertQr extends Plugin {
    init() {
      const editor = this.editor;
      editor.ui.componentFactory.add('promoInsertQr', function (locale) {
        const view = new ButtonView(locale);
        view.set({
          label: 'Insérer un QR code',
          icon: ICON_QR,
          tooltip: true,
        });
        view.on('execute', function () {
          editor.model.change(function (writer) {
            editor.model.insertContent(
              writer.createText('{qrcode="" taille="150" color="#000000" bgcolor="#ffffff"}'),
              editor.model.document.selection
            );
          });
        });
        return view;
      });
    }
  }

  // ── Une instance CKEditor par page promo ────────────────────────────────
  // Map id de conteneur ("wysiwyg-<page.id>") -> instance CKEditor, pour
  // retrouver la bonne instance à l'envoi du formulaire (voir tout en bas).
  const editorInstances = new Map();
  const editorReadyPromises = [];

  document.querySelectorAll('.promo-ck-editor').forEach(function (editorEl) {
    const promise = ClassicEditor.create(editorEl, {
      licenseKey: 'GPL',
      // Traduction française -- voir static/ckeditor5/translations/fr.umd.js
      // (chargé avant ce script, voir admin_slideshow.html).
      language: 'fr',
      placeholder: editorEl.getAttribute('data-placeholder') || '',
      plugins: [
        Essentials, Paragraph, Bold, Italic, Underline,
        FontFamily, FontSize, FontColor, FontBackgroundColor, Alignment,
        List, Table, TableToolbar, TableProperties, TableCellProperties,
        TableColumnResize, Image, ImageToolbar, ImageStyle, ImageResize,
        ImageCaption, RemoveFormat, SourceEditing,
        PromoInsertImage, PromoInsertQr,
      ],
      toolbar: [
        'undo', 'redo', '|',
        'fontFamily', 'fontSize', '|',
        'bold', 'italic', 'underline', '|',
        'fontColor', 'fontBackgroundColor', '|',
        'alignment', 'bulletedList', 'numberedList', '|',
        'insertTable', 'promoInsertImage', 'promoInsertQr', '|',
        'removeFormat', 'sourceEditing',
      ],
      fontFamily: { options: fontFamilyOptions, supportAllValues: true },
      fontSize: { options: fontSizeOptions, supportAllValues: true },
      // v2.0.10 (correctif) : le sélecteur personnalisé (pipette) de
      // couleur de police/surlignage sérialise en HSL par défaut -- forcé
      // en hex ici, cohérent avec tous les autres sélecteurs de couleur de
      // cette page (tous des <input type=color>). Les pastilles de la
      // palette prédéfinie restent en HSL malgré ce réglage (non couvert
      // par colorPicker.format) -- voir sanitize_promo_html (utils.py),
      // qui accepte désormais aussi ce format en secours.
      fontColor: Object.assign(
        { colorPicker: { format: 'hex' } },
        charteColorOptions.length ? { colors: charteColorOptions } : {}
      ),
      fontBackgroundColor: Object.assign(
        { colorPicker: { format: 'hex' } },
        charteColorOptions.length ? { colors: charteColorOptions } : {}
      ),
      table: {
        contentToolbar: [
          'tableColumn', 'tableRow', 'mergeTableCells',
          'tableProperties', 'tableCellProperties',
        ],
      },
      image: {
        toolbar: [
          'imageStyle:alignLeft', 'imageStyle:alignCenter', 'imageStyle:alignRight',
          'imageStyle:side', '|', 'toggleImageCaption', 'resizeImage',
        ],
        styles: {
          options: ['alignLeft', 'alignCenter', 'alignRight', 'side'],
        },
        resizeUnit: 'px',
      },
    }).then(function (editor) {
      editorInstances.set(editorEl.id, editor);

      // Couleur de fond de la zone d'édition (aide visuelle admin
      // uniquement, voir editor_bg_color, jamais exposée à la page
      // publique /bestof) -- posée sur l'élément éditable réel de
      // CKEditor (celui-ci remplace/masque editorEl, voir data-editor-
      // bg-color plutôt que le style inline d'origine, devenu invisible).
      const bgColor = editorEl.getAttribute('data-editor-bg-color');
      const editableEl = editor.ui.getEditableElement();
      if (bgColor && editableEl) editableEl.style.backgroundColor = bgColor;

      return editor;
    });
    editorReadyPromises.push(promise);
  });

  // ── Sélecteur d'image (médiathèque + captures + images dédiées) ─────────
  // Un seul modal partagé par toutes les pages promo (voir
  // admin_slideshow.html → #media-picker-modal). La position d'insertion
  // CKEditor active (LivePosition, survit à toute mutation du modèle
  // pendant que le modal est ouvert) est sauvegardée à l'ouverture et
  // utilisée à l'insertion.
  let _mediaPickerEditor = null;
  let _mediaPickerPosition = null;

  function openMediaPicker(editor) {
    _mediaPickerEditor = editor;
    const sel = editor.model.document.selection;
    _mediaPickerPosition = ModelLivePosition.fromPosition(sel.getLastPosition());
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
      modal.querySelectorAll('.media-picker-panel').forEach(function (g) {
        g.classList.toggle('hidden', g.getAttribute('data-panel') !== wanted);
      });
      return;
    }

    const item = e.target.closest('.media-picker-item');
    if (item && _mediaPickerEditor && _mediaPickerPosition) {
      const url = item.getAttribute('data-url') || '';
      const editor = _mediaPickerEditor;
      const position = _mediaPickerPosition;
      editor.model.change(function (writer) {
        // Image de bloc (comme un paragraphe à part entière) plutôt
        // qu'en ligne dans le texte courant : c'est ce que proposent
        // nativement les boutons d'alignement gauche/droite/côté du
        // widget CKEditor (voir image.toolbar ci-dessus), qui suffisent
        // à obtenir un habillage de texte équivalent à l'ancien
        // redimensionnement/alignement maison, sans configuration
        // supplémentaire.
        const imgElement = writer.createElement('imageBlock', { src: url });
        editor.model.insertObject(imgElement, position, null, { setSelection: 'on' });
      });
      if (position.root) position.detach();
      _mediaPickerPosition = null;
      modal.classList.add('hidden');
      modal.setAttribute('aria-hidden', 'true');
    }
  });

  // Recopie le HTML de l'instance CKEditor de la page dans son champ caché
  // juste avant l'envoi du formulaire (capture=true : passe avant toute
  // autre écoute 'submit').
  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!form.classList || !form.classList.contains('promo-page-form')) return;
    const editorEl = form.querySelector('.promo-ck-editor');
    const hidden = form.querySelector('input[type=hidden][name=html_content]');
    if (!hidden || !editorEl) return;
    const editor = editorInstances.get(editorEl.id);
    if (editor) hidden.value = editor.getData();
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
