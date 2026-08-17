const cfg = window.PICTOTEM || {};

// — Éléments DOM —
const countdown          = document.getElementById('countdown');
const countdownRecording = document.getElementById('countdownRecording');
const recordingSeconds   = document.getElementById('recordingSeconds');
const processingOverlay = document.getElementById('processingOverlay');
const bottomBar        = document.getElementById('bottomActionBar');
const homeBtns         = document.getElementById('homeBtns');
const frameStepBtns    = document.getElementById('frameStepBtns');
const replayPhotoBtns  = document.getElementById('replayPhotoBtns');
const replayVideoBtns  = document.getElementById('replayVideoBtns');
const resultShell      = document.getElementById('resultShell');
const resultMedia      = document.getElementById('resultMedia');
const resultMessage    = document.getElementById('resultMessage');
const replayBadge      = document.getElementById('replayBadge');
const welcomeOverlayEl = document.getElementById('welcomeOverlayEl');
const frameOverlayImg  = document.getElementById('frameOverlayImg');
const framePickerPanel = document.getElementById('framePickerPanel');
const pickerStrip      = document.getElementById('pickerStrip');
const pickerDragHandle = document.getElementById('pickerDragHandle');
const btnTakePhoto     = document.getElementById('btnTakePhoto');
const btnTakeVideo     = document.getElementById('btnTakeVideo');
const btnTakeStrip     = document.getElementById('btnTakeStrip');
const btnChooseFrame   = document.getElementById('btnChooseFrame');
const btnStartCapture  = document.getElementById('btnStartCapture');
const btnFrameStepBack = document.getElementById('btnFrameStepBack');
const btnBackPhoto     = document.getElementById('btnBackPhoto');
const btnBackVideo     = document.getElementById('btnBackVideo');
const btnRetakePhoto   = document.getElementById('btnRetakePhoto');
const btnRetakeVideo   = document.getElementById('btnRetakeVideo');
const btnPrintPhoto    = document.getElementById('btnPrintPhoto');
const lookHereLabel    = document.getElementById('lookHereLabel');
const btnFramePickerBack  = document.getElementById('btnFramePickerBack');
const btnFramePickerApply = document.getElementById('btnFramePickerApply');
const btnPickerLeft  = document.getElementById('btnPickerLeft');
const btnPickerRight = document.getElementById('btnPickerRight');
const lookHereBanner   = document.getElementById('lookHereBanner');
const bottomLeftMessage  = document.getElementById('bottomLeftMessage');
const bottomRightMessage = document.getElementById('bottomRightMessage');
const idleTimerEl    = document.getElementById('idleTimerEl');
const idleTimerBadge = document.getElementById('idleTimerBadge');
const topCountdownBar     = document.getElementById('topCountdownBar');
const topCountdownBarFill = document.getElementById('topCountdownBarFill');

// — Barre de progression partagée (tout en haut de l'écran) —
// Un seul élément, réutilisé par tous les comptes à rebours de l'appli (timer
// de veille, décompte avant capture, enregistrement vidéo, retour auto à
// l'accueil) : un seul est actif à la fois, donc aucun conflit. Le rétrécissement
// est géré nativement par une transition CSS (fluide, quelle que soit la durée),
// pas par un setInterval qui recalcule une largeur toutes les secondes.
function startTopBarCountdown(durationMs) {
  if (!topCountdownBar || !topCountdownBarFill || !(durationMs > 0)) return;
  topCountdownBarFill.style.transition = 'none';
  topCountdownBarFill.style.width = '100%';
  void topCountdownBarFill.offsetWidth; // force le reflow avant de relancer la transition
  topCountdownBarFill.style.transition = `width ${durationMs / 1000}s linear`;
  topCountdownBarFill.style.width = '0%';
  setVisible(topCountdownBar, true);
}

function stopTopBarCountdown() {
  if (!topCountdownBar || !topCountdownBarFill) return;
  setVisible(topCountdownBar, false);
  topCountdownBarFill.style.transition = 'none';
  topCountdownBarFill.style.width = '100%';
}

// — Timer d'inactivité (retour auto à l'accueil) —
const IDLE_ACTIVE_STATES = new Set(['frame-step', 'frame-picker', 'replay']);
let idleTimer     = null;
let idleRemaining = 0;
let idleTotal     = 0;

// — État —
let state         = 'home';
let captureMode   = null;
let selectedFrame = cfg.defaultFrame || 'none'; // cadre validé pour la capture
let pendingFrame  = selectedFrame;               // cadre en cours de prévisualisation
let replayCapture = null;
let countdownTimer          = null;
let recordingTimer          = null;
let recordingTransitionTimer = null;

// — Drag du picker —
let isDragging      = false;
let dragStartY      = 0;
let dragStartHeight = 220;
const PICKER_MIN    = 120;
const PICKER_MAX    = () => Math.round(window.innerHeight * 0.65);

// — Utilitaires —
function setVisible(el, visible) { if (el) el.classList.toggle('hidden', !visible); }
function showBottomBar(show) { setVisible(bottomBar, show); }
function showResult(show)    { setVisible(resultShell, show); }
function showProcessing(show){ setVisible(processingOverlay, show); }
function showReplayBadge(show){ setVisible(replayBadge, show); }
function showLookHere(show)  { setVisible(lookHereBanner, show); }
function showPicker(show)    { setVisible(framePickerPanel, show); }

function setBottomCenterState(which) {
  setVisible(homeBtns,       which === 'home');
  setVisible(frameStepBtns,  which === 'frame-step');
  setVisible(replayPhotoBtns, which === 'replay-photo');
  setVisible(replayVideoBtns, which === 'replay-video');
}

function setBottomMessages(left, right) {
  if (bottomLeftMessage)  bottomLeftMessage.textContent  = left  || '';
  if (bottomRightMessage) bottomRightMessage.textContent = right || '';
}

function setState(next) {
  state = next;
  document.body.dataset.state = next;
  // Sécurité : la barre de veille (voir plus bas) ne doit jamais rester
  // affichée en dehors de l'accueil — le prochain compte à rebours éventuel
  // (idle-timer, capture...) la relance lui-même via startTopBarCountdown().
  if (next !== 'home') stopTopBarCountdown();
}

// — Timer d'inactivité —
function _idleUpdateUi() {
  if (idleTimerBadge) {
    const tpl = cfg.idleTimerBadgeText || 'Retour dans {n}s';
    idleTimerBadge.textContent = tpl.replace('{n}', idleRemaining);
  }
}

function _idleApplyStyle() {
  if (!idleTimerBadge) return;
  if (cfg.idleTimerFontSize)  idleTimerBadge.style.fontSize = `${cfg.idleTimerFontSize}px`;
  if (cfg.idleTimerPaddingY != null && cfg.idleTimerPaddingX != null)
    idleTimerBadge.style.padding = `${cfg.idleTimerPaddingY}px ${cfg.idleTimerPaddingX}px`;
}

function startIdleTimer() {
  if (!cfg.idleTimerEnabled || !cfg.idleTimerSeconds) return;
  _idleApplyStyle();
  clearInterval(idleTimer);
  idleTotal     = cfg.idleTimerSeconds;
  idleRemaining = idleTotal;
  _idleUpdateUi();
  setVisible(idleTimerEl, true);
  startTopBarCountdown(idleTotal * 1000);
  idleTimer = setInterval(() => {
    idleRemaining -= 1;
    _idleUpdateUi();
    if (idleRemaining <= 0) {
      stopIdleTimer();
      applyHomeUi();
    }
  }, 1000);
}

function stopIdleTimer() {
  clearInterval(idleTimer);
  idleTimer = null;
  setVisible(idleTimerEl, false);
  stopTopBarCountdown();
}

function resetIdleTimer() {
  if (!cfg.idleTimerEnabled || !IDLE_ACTIVE_STATES.has(state)) return;
  startIdleTimer();
}

// Reset sur tout clic/touch (le handler de chaque bouton gère ensuite la transition d'état)
document.addEventListener('click',      resetIdleTimer, { capture: true });
document.addEventListener('touchstart', resetIdleTimer, { capture: true, passive: true });

// — Écran de veille (screensaver) —
// Distinct du timer d'inactivité ci-dessus (qui ne fait que ramener à l'accueil
// pendant une capture en cours). Ici : après cfg.screensaverTimeoutSeconds sans
// AUCUNE interaction alors qu'on est sur l'écran d'accueil, on affiche un
// diaporama plein écran (photos/vidéos de la borne + images dédiées gérées
// dans /admin/screensaver, via /api/screensaver/slides). La toute première
// interaction referme la veille et est absorbée par l'overlay (elle ne
// déclenche jamais un bouton caché dessous).
const screensaverOverlay = document.getElementById('screensaverOverlay');
const screensaverSlideA  = document.getElementById('screensaverSlideA');
const screensaverSlideB  = document.getElementById('screensaverSlideB');

let ssLastActivity  = Date.now();
let ssActive        = false;
let ssSlides        = [];
let ssIdx           = 0;
let ssCurrent       = 'A';
let ssCleanup       = null;
let ssRefreshTimer  = null;
let ssDelayMs       = 5000;

function ssShuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
}

function ssInterleave(captures, images) {
  if (!images.length) return [...captures];
  if (!captures.length) return [...images];
  const result = [];
  const step = Math.max(1, Math.floor(captures.length / (images.length + 1)));
  let ii = 0;
  captures.forEach((c, i) => {
    result.push(c);
    if ((i + 1) % step === 0 && ii < images.length) result.push(images[ii++]);
  });
  while (ii < images.length) result.push(images[ii++]);
  return result;
}

async function ssFetchSlides() {
  const res = await fetch('/api/screensaver/slides');
  return res.json();
}

function ssShowNext() {
  if (!ssActive || !ssSlides.length) return;
  if (ssCleanup) { ssCleanup(); ssCleanup = null; }

  const slide = ssSlides[ssIdx];
  ssIdx++;
  if (ssIdx >= ssSlides.length) { ssIdx = 0; ssShuffle(ssSlides); }

  const next  = ssCurrent === 'A' ? 'B' : 'A';
  const curEl  = ssCurrent === 'A' ? screensaverSlideA : screensaverSlideB;
  const nextEl = next === 'A' ? screensaverSlideA : screensaverSlideB;
  nextEl.innerHTML = '';

  let called = false;
  const advance = () => { if (!called) { called = true; ssShowNext(); } };

  if (slide.type === 'video') {
    const v = document.createElement('video');
    v.src = slide.url;
    v.muted = true;
    v.autoplay = true;
    v.playsInline = true;
    v.addEventListener('ended', advance);
    v.addEventListener('error', advance);
    const guard = setTimeout(advance, 120_000); // sécurité 2 min max
    ssCleanup = () => { clearTimeout(guard); v.pause(); };
    nextEl.appendChild(v);
  } else {
    const el = document.createElement('img');
    el.src = slide.url;
    el.onerror = advance;
    nextEl.appendChild(el);
    const t = setTimeout(advance, ssDelayMs);
    ssCleanup = () => clearTimeout(t);
  }

  requestAnimationFrame(() => {
    nextEl.classList.add('active');
    setTimeout(() => curEl.classList.remove('active'), 800);
  });
  ssCurrent = next;
}

async function ssStart() {
  if (ssActive) return;
  ssActive = true;
  stopTopBarCountdown(); // le diaporama prend le relais, plus besoin du compte à rebours
  setVisible(screensaverOverlay, true);
  try {
    const data = await ssFetchSlides();
    ssDelayMs = (data.delay || 5) * 1000;
    const all = ssInterleave(data.captures || [], data.screensaver_images || []);
    ssSlides = all;
    ssIdx = 0;
    if (ssSlides.length) ssShowNext();
  } catch (e) {
    console.warn('[screensaver] chargement des slides échoué', e);
  }
  ssRefreshTimer = setInterval(async () => {
    try {
      const data = await ssFetchSlides();
      const all = ssInterleave(data.captures || [], data.screensaver_images || []);
      if (all.length) ssSlides = all;
    } catch (e) { /* on garde la liste actuelle */ }
  }, 60_000);
}

function ssStop() {
  if (!ssActive) return;
  ssActive = false;
  setVisible(screensaverOverlay, false);
  if (ssCleanup) { ssCleanup(); ssCleanup = null; }
  clearInterval(ssRefreshTimer);
  ssRefreshTimer = null;
  screensaverSlideA.innerHTML = '';
  screensaverSlideB.innerHTML = '';
  screensaverSlideA.classList.remove('active');
  screensaverSlideB.classList.remove('active');
  ssCurrent = 'A';
}

// Relance la barre de progression partagée pour le compte à rebours de
// veille — appelée à l'arrivée sur l'accueil et à chaque activité (throttlée
// pour mousemove, qui peut se déclencher très souvent).
let ssBarLastMouseRestart = 0;
function startVeilleTopBar() {
  if (!cfg.screensaverEnabled || !cfg.screensaverTimeoutSeconds) return;
  if (state !== 'home' || ssActive) return;
  startTopBarCountdown(cfg.screensaverTimeoutSeconds * 1000);
}

function ssNoteActivity(e) {
  ssLastActivity = Date.now();
  if (ssActive) ssStop(); // referme la veille ; on retombe plus bas pour relancer la barre
  if (state !== 'home') return;
  if (e && e.type === 'mousemove') {
    const now = Date.now();
    if (now - ssBarLastMouseRestart < 1000) return;
    ssBarLastMouseRestart = now;
  }
  startVeilleTopBar();
}

function ssCheck() {
  if (!cfg.screensaverEnabled || !cfg.screensaverTimeoutSeconds) return;
  if (ssActive) return;
  if (state !== 'home') { ssLastActivity = Date.now(); return; } // jamais couper une capture en cours
  if ((Date.now() - ssLastActivity) / 1000 >= cfg.screensaverTimeoutSeconds) ssStart();
}

// Écouteurs + boucle de vérification toujours actifs (même si la veille est
// désactivée au chargement) : startVeilleTopBar()/ssCheck() relisent
// cfg.screensaverEnabled à chaque appel, donc un changement fait dans
// /admin/screensaver s'applique sans recharger la page — voir ssRefreshSettings.
['mousemove', 'mousedown', 'touchstart', 'keydown', 'click'].forEach((evt) => {
  document.addEventListener(evt, ssNoteActivity, { capture: true, passive: true });
});
setInterval(ssCheck, 2000);

// Relit périodiquement l'état réel (activé/délai) depuis le serveur pour
// appliquer un changement fait dans le back office sans reload de page.
async function ssRefreshSettings() {
  try {
    const res = await fetch('/api/screensaver/settings');
    const data = await res.json();
    const wasEnabled = cfg.screensaverEnabled;
    cfg.screensaverEnabled = !!data.enabled;
    cfg.screensaverTimeoutSeconds = data.timeout_seconds || 0;
    if (!cfg.screensaverEnabled) {
      if (ssActive) ssStop();
      stopTopBarCountdown();
    } else if (!wasEnabled) {
      ssLastActivity = Date.now();
      startVeilleTopBar();
    }
  } catch (e) { /* on garde les valeurs actuelles */ }
}
setInterval(ssRefreshSettings, 30000);

// — Masquage automatique de la barre inférieure sur l'écran d'accueil —
const HOME_BAR_HIDE_MS = 10000;
let homeBarTimer = null;

function _clearHomeBarHide() {
  clearTimeout(homeBarTimer);
  homeBarTimer = null;
  if (bottomBar) bottomBar.classList.remove('bottom-bar-collapsed');
}

function _revealHomeBar() {
  if (bottomBar) bottomBar.classList.remove('bottom-bar-collapsed');
  clearTimeout(homeBarTimer);
  homeBarTimer = setTimeout(() => {
    if (state === 'home' && bottomBar) bottomBar.classList.add('bottom-bar-collapsed');
  }, HOME_BAR_HIDE_MS);
}

document.addEventListener('mousemove',  () => { if (state === 'home') _revealHomeBar(); }, { passive: true });
document.addEventListener('touchstart', () => { if (state === 'home') _revealHomeBar(); }, { passive: true });

// — Overlay cadre —
function getOverlayUrl(frameId) {
  if (!frameId || frameId === 'none' || !cfg.frames) return '';
  const f = cfg.frames.find(f => f.id === frameId);
  return f?.overlay || '';
}

function showWelcomeOverlay(show) {
  if (!welcomeOverlayEl) return;
  if (show && cfg.welcomeFrame) {
    welcomeOverlayEl.style.backgroundImage = `url('${cfg.welcomeFrame}')`;
    welcomeOverlayEl.classList.remove('hidden');
  } else {
    welcomeOverlayEl.classList.add('hidden');
  }
}

function showOverlayFor(frameId) {
  if (!frameOverlayImg) return;
  const url = getOverlayUrl(frameId);
  if (url) {
    frameOverlayImg.style.backgroundImage = `url('${url}')`;
    frameOverlayImg.classList.remove('hidden');
  } else {
    frameOverlayImg.classList.add('hidden');
    frameOverlayImg.style.backgroundImage = '';
  }
}

function updateFrameOverlay(visible) {
  if (!visible) {
    if (frameOverlayImg) {
      frameOverlayImg.classList.add('hidden');
      frameOverlayImg.style.backgroundImage = '';
    }
    return;
  }
  showOverlayFor(selectedFrame);
}

// — Picker strip —
function renderPickerStrip() {
  if (!pickerStrip) return;
  pickerStrip.innerHTML = '';
  (cfg.frames || []).forEach(frame => {
    const btn = document.createElement('button');
    btn.className = 'picker-frame-btn' + (frame.id === pendingFrame ? ' active' : '');
    btn.dataset.frameId = frame.id;
    if (frame.preview) {
      const img = document.createElement('img');
      img.src = frame.preview;
      img.alt = frame.label;
      btn.appendChild(img);
    } else {
      const ph = document.createElement('div');
      ph.className = 'picker-no-preview';
      ph.textContent = frame.label;
      btn.appendChild(ph);
    }
    btn.addEventListener('click', () => {
      pendingFrame = frame.id;
      pickerStrip.querySelectorAll('.picker-frame-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.frameId === pendingFrame)
      );
      showOverlayFor(pendingFrame);
    });
    pickerStrip.appendChild(btn);
  });
}

// — États UI —
function showFrameStepBack(show) {
  setVisible(btnFrameStepBack, show);
  setVisible(bottomLeftMessage, !show);
}

function applyHomeUi() {
  stopIdleTimer();
  setState('home');
  captureMode = null;
  showBottomBar(true);
  showPicker(false);
  setBottomCenterState('home');
  showFrameStepBack(false);
  showResult(false);
  showProcessing(false);
  showReplayBadge(false);
  showLookHere(false);
  updateFrameOverlay(false);
  showWelcomeOverlay(true);
  setBottomMessages(cfg.bottomLeftHome || 'Touchez un bouton pour lancer une capture', cfg.bottomRight || '');
  _revealHomeBar();
  startVeilleTopBar();
}

function applyFrameStepUi(mode) {
  _clearHomeBarHide();
  setState('frame-step');
  startIdleTimer();
  captureMode = mode;
  showBottomBar(true);
  showPicker(false);
  setBottomCenterState('frame-step');
  showFrameStepBack(true);
  showResult(false);
  showProcessing(false);
  showReplayBadge(false);
  showLookHere(false);
  showWelcomeOverlay(false);
  updateFrameOverlay(true);
  setBottomMessages(cfg.bottomLeftFrame || 'Choisissez un cadre puis lancez la capture', cfg.bottomRight || '');
  if (btnStartCapture) btnStartCapture.textContent = cfg.startBtnText || "C'est parti";
}

async function applyFramePickerUi() {
  _clearHomeBarHide();
  setState('frame-picker');
  startIdleTimer();
  pendingFrame = selectedFrame;
  showBottomBar(false);
  showPicker(true);
  showResult(false);
  showProcessing(false);
  showReplayBadge(false);
  showLookHere(false);
  showWelcomeOverlay(false);
  showOverlayFor(pendingFrame);
  try {
    const data = await fetch('/api/frames').then(r => r.json());
    if (Array.isArray(data.frames)) cfg.frames = data.frames;
    if (data.default_frame) cfg.defaultFrame = data.default_frame;
  } catch (_) { /* garde les cadres existants en cas d'erreur */ }
  renderPickerStrip();
}

function applyReplayUi(kind, capture) {
  _clearHomeBarHide();
  setState('replay');
  startIdleTimer();
  captureMode = kind;
  replayCapture = capture;
  showBottomBar(true);
  showPicker(false);
  // Le photo strip ('strip') produit une image statique comme 'photo' :
  // même écran de relecture (imprimable), contrairement à 'video'.
  setBottomCenterState((kind === 'photo' || kind === 'strip') ? 'replay-photo' : 'replay-video');
  showFrameStepBack(false);
  showResult(true);
  showReplayBadge(true);
  showLookHere(false);
  showWelcomeOverlay(false);
  updateFrameOverlay(false);
  setBottomMessages(cfg.bottomLeftReplay || 'Votre capture est prête', cfg.bottomRight || '');
}

function applyCountdownUi() {
  _clearHomeBarHide();
  stopIdleTimer();
  showBottomBar(false);
  showPicker(false);
  setBottomCenterState('none');
  showResult(false);
  showReplayBadge(false);
  showLookHere(true);
  showProcessing(false);
  updateFrameOverlay(true);
}

// — API —
async function api(path, payload = null) {
  const opt = { method: 'POST', headers: { 'Content-Type': 'application/json' } };
  if (payload) opt.body = JSON.stringify(payload);
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function renderResultMedia(data) {
  if (!resultMedia) return;
  resultMedia.innerHTML = '';
  if (data.kind === 'photo') {
    const img = document.createElement('img');
    img.src = data.url;
    img.alt = 'Photo capturée';
    img.style.cssText = 'width:100%;height:100%;object-fit:contain';
    resultMedia.appendChild(img);
  } else {
    const video = document.createElement('video');
    video.muted = true;       // muted AVANT src pour autoriser l'autoplay
    video.loop = true;
    video.controls = true;
    video.playsInline = true;
    video.style.cssText = 'width:100%;height:100%;object-fit:contain';
    video.addEventListener('canplay', () => {
      video.play().catch(() => {});
    }, { once: true });
    video.src = data.url;
    video.load();
    resultMedia.appendChild(video);
  }
}

function startRecordingCounter(durationSec) {
  clearInterval(recordingTimer);
  let rem = durationSec;
  if (recordingSeconds) recordingSeconds.textContent = `${rem}s`;
  setVisible(countdownRecording, true);
  startTopBarCountdown(durationSec * 1000);
  recordingTimer = setInterval(() => {
    rem -= 1;
    if (rem <= 0) {
      clearInterval(recordingTimer);
      if (recordingSeconds) recordingSeconds.textContent = '…';
      // Enregistrement terminé → spinner pendant le traitement ffmpeg
      recordingTransitionTimer = setTimeout(() => {
        setVisible(countdownRecording, false);
        showProcessing(true);
      }, 600);
      return;
    }
    if (recordingSeconds) recordingSeconds.textContent = `${rem}s`;
  }, 1000);
}

function stopRecordingCounter() {
  clearInterval(recordingTimer);
  clearTimeout(recordingTransitionTimer);
  setVisible(countdownRecording, false);
  stopTopBarCountdown();
}

async function startCountdownAndCapture(kind) {
  clearInterval(countdownTimer);
  let remaining = Number(cfg.countdownSeconds || 3);

  // Phase 1 : décompte pré-capture (centré, grand, "regardez ici" visible)
  applyCountdownUi();
  setVisible(countdown, true);
  countdown.textContent = String(remaining);
  startTopBarCountdown(remaining * 1000);

  countdownTimer = setInterval(async () => {
    remaining -= 1;
    if (remaining > 0) { countdown.textContent = String(remaining); return; }

    // Fin du décompte pré-capture
    clearInterval(countdownTimer);
    setVisible(countdown, false);
    showLookHere(false);

    const payload = { frame: selectedFrame };
    let stripTicker = null;

    if (kind === 'video') {
      // Phase 2 vidéo : overlay reste visible pendant l'enregistrement
      // (appliqué côté serveur sur la vidéo finale) — startRecordingCounter()
      // relance la barre de progression pour la durée d'enregistrement.
      const dur = cfg.videoDurationSec || 5;
      payload.duration = dur;
      startRecordingCounter(dur);
    } else if (kind === 'strip') {
      // Photo strip : le serveur prend plusieurs clichés à la suite (voir
      // capture.photo_strip dans config.toml) — indicateur visuel "Photo
      // X/N" purement côté client, calé sur le même rythme (shots ×
      // interval_sec) pour rester synchro avec la prise réelle côté serveur.
      const shots = Number(cfg.photoStripShots || 3);
      const intervalMs = Number(cfg.photoStripIntervalSec || 1.2) * 1000;
      let shotN = 1;
      showLookHere(true);
      if (lookHereLabel) lookHereLabel.textContent = `Photo ${shotN}/${shots}`;
      startTopBarCountdown(shots * intervalMs);
      stripTicker = setInterval(() => {
        shotN += 1;
        if (shotN > shots) { clearInterval(stripTicker); return; }
        if (lookHereLabel) lookHereLabel.textContent = `Photo ${shotN}/${shots}`;
      }, intervalMs);
    } else {
      // Photo : on cache l'overlay et on montre le spinner (pas de durée
      // connue pendant le traitement serveur, donc pas de barre à ce stade)
      stopTopBarCountdown();
      updateFrameOverlay(false);
      showProcessing(true);
    }

    const apiPath = kind === 'photo' ? '/api/capture/photo'
                  : kind === 'video' ? '/api/capture/video'
                  : '/api/capture/photostrip';

    try {
      const data = await api(apiPath, payload);
      if (kind === 'video') {
        stopRecordingCounter();
        updateFrameOverlay(false);  // cache l'overlay maintenant que la vidéo est capturée
      } else if (kind === 'strip') {
        clearInterval(stripTicker);
        stopTopBarCountdown();
        showLookHere(false);
        updateFrameOverlay(false);
      }
      showProcessing(false);
      renderResultMedia(data);
      if (resultMessage) resultMessage.textContent = data.message || '';
      applyReplayUi(kind, data);
    } catch (e) {
      if (kind === 'video') { stopRecordingCounter(); updateFrameOverlay(false); }
      if (kind === 'strip') { clearInterval(stripTicker); showLookHere(false); updateFrameOverlay(false); }
      stopTopBarCountdown();
      showProcessing(false);
      applyHomeUi();
      alert(e.message || 'Erreur de capture');
    }
  }, 1000);
}

// — Drag du picker —
function pickerStartDrag(e) {
  isDragging = true;
  const pos = e.touches ? e.touches[0] : e;
  dragStartY = pos.clientY;
  dragStartHeight = framePickerPanel?.offsetHeight || 220;
  e.preventDefault();
}
function pickerOnDrag(e) {
  if (!isDragging || !framePickerPanel) return;
  const pos = e.touches ? e.touches[0] : e;
  const delta = dragStartY - pos.clientY;
  const newH = Math.max(PICKER_MIN, Math.min(PICKER_MAX(), dragStartHeight + delta));
  framePickerPanel.style.height = `${newH}px`;
  e.preventDefault();
}
function pickerEndDrag() { isDragging = false; }

pickerDragHandle?.addEventListener('mousedown',  pickerStartDrag);
pickerDragHandle?.addEventListener('touchstart', pickerStartDrag, { passive: false });
document.addEventListener('mousemove',  pickerOnDrag);
document.addEventListener('touchmove',  pickerOnDrag, { passive: false });
document.addEventListener('mouseup',    pickerEndDrag);
document.addEventListener('touchend',   pickerEndDrag);

// — Handlers boutons —
btnTakePhoto?.addEventListener('click', () => { captureMode = 'photo'; applyFrameStepUi('photo'); });
btnTakeVideo?.addEventListener('click', () => { captureMode = 'video'; applyFrameStepUi('video'); });
btnTakeStrip?.addEventListener('click', () => { captureMode = 'strip'; applyFrameStepUi('strip'); });

btnChooseFrame?.addEventListener('click', applyFramePickerUi);

function scrollPicker(dir) {
  if (!pickerStrip) return;
  const card = pickerStrip.querySelector('.picker-frame-btn');
  const step = card ? card.offsetWidth + 10 : 220;
  pickerStrip.scrollBy({ left: dir * step, behavior: 'smooth' });
}
btnPickerLeft?.addEventListener('click',  () => scrollPicker(-1));
btnPickerRight?.addEventListener('click', () => scrollPicker(1));

btnFramePickerBack?.addEventListener('click', () => {
  pendingFrame = selectedFrame; // annule les changements
  applyFrameStepUi(captureMode);
});

btnFramePickerApply?.addEventListener('click', () => {
  selectedFrame = pendingFrame; // valide le cadre choisi
  applyFrameStepUi(captureMode);
});

btnStartCapture?.addEventListener('click', () => {
  if (captureMode) startCountdownAndCapture(captureMode);
});

btnFrameStepBack?.addEventListener('click', applyHomeUi);
btnBackPhoto?.addEventListener('click', applyHomeUi);
btnBackVideo?.addEventListener('click', applyHomeUi);

// — Recommencer : supprime la capture tout juste prise et relance une prise
// de vue immédiatement (même mode, même cadre) — évite d'accumuler des
// essais ratés dans la galerie. La suppression est best-effort : en cas
// d'échec réseau on relance quand même la capture (ne jamais bloquer le
// visiteur pour un souci de nettoyage).
async function doRetake() {
  const mode = captureMode;
  const capId = replayCapture?.id;
  if (capId) {
    try { await api(`/api/capture/${capId}/retake`); }
    catch (e) { console.warn('[recommencer] suppression échouée', e); }
  }
  applyFrameStepUi(mode || 'photo');
}
btnRetakePhoto?.addEventListener('click', doRetake);
btnRetakeVideo?.addEventListener('click', doRetake);

btnPrintPhoto?.addEventListener('click', async () => {
  if (!replayCapture) return;
  btnPrintPhoto.disabled = true;
  try {
    await api(`/api/print/${replayCapture.id}`);
  } catch (e) {
    alert(e.message || 'Erreur impression');
  } finally {
    btnPrintPhoto.disabled = false;
  }
});

// — Plein écran (fenêtre native) : sortie/entrée protégée par mot de passe —
// Déclencheurs : 5 appuis rapides dans le coin haut-gauche (kioskUnlockZone),
// ou Ctrl+Maj+K. Sans effet hors de la fenêtre native (pywebview absent).
// Modale HTML intégrée à la page plutôt que prompt()/alert() natifs : ces
// derniers peuvent rester masqués derrière une fenêtre plein écran
// "always on top", donnant l'impression que rien ne se passe.
const kioskUnlockModal   = document.getElementById('kioskUnlockModal');
const kioskUnlockInput   = document.getElementById('kioskUnlockInput');
const kioskUnlockMsg     = document.getElementById('kioskUnlockMsg');
const kioskUnlockConfirm = document.getElementById('kioskUnlockConfirm');
const kioskUnlockCancel  = document.getElementById('kioskUnlockCancel');

function _openUnlockModal() {
  if (!window.pywebview || !window.pywebview.api) {
    console.warn('[kiosk-unlock] window.pywebview.api indisponible (hors fenêtre native ?)');
    return;
  }
  if (!kioskUnlockModal) return;
  if (kioskUnlockMsg) kioskUnlockMsg.textContent = '';
  if (kioskUnlockInput) kioskUnlockInput.value = '';
  kioskUnlockModal.classList.remove('hidden');
  setTimeout(() => kioskUnlockInput?.focus(), 50);
}

function _closeUnlockModal() {
  kioskUnlockModal?.classList.add('hidden');
}

function _submitUnlock() {
  const pwd = kioskUnlockInput ? kioskUnlockInput.value : '';
  if (kioskUnlockConfirm) kioskUnlockConfirm.disabled = true;
  if (kioskUnlockMsg) kioskUnlockMsg.textContent = 'Vérification…';
  console.log('[kiosk-unlock] appel toggle_fullscreen...');
  window.pywebview.api.toggle_fullscreen(pwd).then(res => {
    console.log('[kiosk-unlock] réponse:', res);
    if (kioskUnlockConfirm) kioskUnlockConfirm.disabled = false;
    if (res && res.ok) {
      _closeUnlockModal();
    } else if (kioskUnlockMsg) {
      kioskUnlockMsg.textContent = (res && res.error) || 'Erreur.';
    }
  }).catch(err => {
    console.error('[kiosk-unlock] erreur d\'appel:', err);
    if (kioskUnlockConfirm) kioskUnlockConfirm.disabled = false;
    if (kioskUnlockMsg) kioskUnlockMsg.textContent = 'Erreur de communication.';
  });
}

kioskUnlockConfirm?.addEventListener('click', _submitUnlock);
kioskUnlockCancel?.addEventListener('click', _closeUnlockModal);
kioskUnlockInput?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); _submitUnlock(); }
  if (e.key === 'Escape') { e.preventDefault(); _closeUnlockModal(); }
});

(function setupKioskUnlock() {
  const zone = document.getElementById('kioskUnlockZone');
  const TAP_COUNT = 5;
  const TAP_WINDOW_MS = 3000;
  let taps = [];
  zone?.addEventListener('click', () => {
    const now = Date.now();
    taps = taps.filter(t => now - t < TAP_WINDOW_MS);
    taps.push(now);
    if (taps.length >= TAP_COUNT) {
      taps = [];
      _openUnlockModal();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      _openUnlockModal();
    }
  });
})();

// — Init —
applyHomeUi();
