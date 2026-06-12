// static/js/voiceConversation.js
//
// Hands-free, near-realtime two-way voice chat ("conversation mode").
//
// Wires together the pieces that already exist — VAD-gated recording
// (voiceRecorder.js), streaming STT (/api/stt), the streaming LLM
// (chat.js / /api/chat_stream) and sentence-by-sentence TTS (tts-ai.js) —
// into a continuous loop:
//
//     listening → (silence) → thinking → speaking → listening …
//
// Plus barge-in: while J.A.R.V.I.S is speaking, a lightweight mic monitor
// listens for the user starting to talk and interrupts playback so the user
// never has to wait for a long answer to finish.
//
// The transport is unchanged (HTTP + SSE); this module is pure orchestration.

import voiceRecorder from './voiceRecorder.js';

const STATE = {
  OFF: 'off',
  LISTENING: 'listening',
  THINKING: 'thinking',
  SPEAKING: 'speaking',
};

let _enabled = false;
let _state = STATE.OFF;
let _watchTimer = null;     // polls for stream/TTS completion to re-arm
let _barge = null;          // active barge-in monitor { stop() }
let _prevAutoPlay = false;   // restore TTS autoplay on exit
let _stream = null;          // persistent mic stream, reused across all turns

// Latency tuning. The endpointing silence is the single biggest knob for
// perceived responsiveness — how long after you stop talking before J.A.R.V.I.S
// replies. Kept short for near-realtime; bump if it clips the ends of words.
const VAD_TUNING = {
  vadThreshold: 0.005,  // RMS gate; tuned for raw (un-AGC'd) mic levels
  silenceMs: 500,       // trailing silence that ends a turn
  minSpeechMs: 100,     // min voiced run to count as speech (rejects clicks)
  maxMs: 15000,         // hard cap on a single utterance
};

// Barge-in (interrupting J.A.R.V.I.S by talking over it) monitors the RAW
// mic stream and calibrates against what it hears while TTS is playing. An
// echo-cancelled stream was tried first and failed: Chrome's AEC suppresses
// the near end during double-talk, crushing the user's voice exactly when
// they try to interrupt. On a headset the raw mic hears (near) nothing of
// the TTS, so the trigger sits just above the noise floor; on open speakers
// the calibration measures the acoustic echo and raises the trigger above
// it (barging in then requires speaking louder than the echo).
const BARGE_ENABLED = true;

// Injected by app.js — see configure().
let _ui = {
  submit: null,        // () => trigger a chat submit using #message contents
  showToast: null,
  showError: null,
  onState: null,       // (state) => update the toolbar button
};

export function isActive() { return _enabled; }
export function getState() { return _state; }

export function configure(opts) {
  _ui = Object.assign(_ui, opts || {});
}

function _setState(s) {
  _state = s;
  console.log('[convo] state:', s);
  if (_ui.onState) { try { _ui.onState(s); } catch (e) { /* ignore */ } }
}

function _sttReady() {
  const p = voiceRecorder._sttProvider;
  return !!p && p !== 'disabled';
}

function _ttsBusy() {
  const t = window.aiTTSManager;
  if (!t) return false;
  return !!(t.isPlaying || t._processing || t._streamActive ||
            (t._queue && t._queue.length > 0));
}

function _isStreaming() {
  // The send button reflects the active LLM stream (set to 'streaming' in
  // chat.js / app.js while a response is in flight).
  const sb = document.querySelector('.send-btn');
  return !!(sb && sb.dataset.mode === 'streaming');
}

// ── Public controls ──────────────────────────────────────────────────────

export async function start() {
  if (_enabled) return;
  if (!_sttReady()) {
    if (_ui.showError) _ui.showError('Enable speech-to-text in Settings to use conversation mode.');
    return;
  }
  // Acquire the mic once up front and hold it for the whole conversation —
  // re-acquiring per turn costs ~100-300ms each time we re-arm. Echo
  // cancellation keeps J.A.R.V.I.S's own TTS out of the mic (for barge-in).
  try {
    // RAW audio (all processing off). This keeps the silence floor near zero,
    // which is what end-of-speech detection needs — autoGainControl would pump
    // the noise floor up and break silence detection, and noiseSuppression can
    // gate quiet speech away. The VAD threshold is tuned low to match raw levels.
    _stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    const tr = _stream.getAudioTracks()[0];
    if (tr) console.log('[convo] mic:', tr.label, JSON.stringify(tr.getSettings ? tr.getSettings() : {}));
  } catch (e) {
    if (_ui.showError) {
      _ui.showError(e && e.name === 'NotAllowedError'
        ? 'Microphone access denied. Check browser permissions.'
        : 'Could not open the microphone.');
    }
    return;
  }
  _enabled = true;
  // Force TTS autoplay so replies are spoken, and remember the prior value so
  // toggling conversation mode off doesn't clobber the user's TTS preference.
  if (window.aiTTSManager) {
    _prevAutoPlay = window.aiTTSManager.autoPlay;
    window.aiTTSManager.autoPlay = true;
  }
  _listen();
}

export function stop() {
  if (!_enabled) return;
  _enabled = false;
  if (_watchTimer) { clearInterval(_watchTimer); _watchTimer = null; }
  _stopBarge();
  try { voiceRecorder.stopRecording(); } catch (e) { /* ignore */ }
  if (window.aiTTSManager) {
    try { window.aiTTSManager.stop(); } catch (e) { /* ignore */ }
    window.aiTTSManager.autoPlay = _prevAutoPlay;
  }
  if (_stream) {
    _stream.getTracks().forEach(t => t.stop());
    _stream = null;
  }
  _setState(STATE.OFF);
}

export function toggle() { _enabled ? stop() : start(); }

// ── Loop ─────────────────────────────────────────────────────────────────

function _listen() {
  if (!_enabled) return;
  _stopBarge();
  _setState(STATE.LISTENING);
  voiceRecorder.startRecording(
    null,            // onFileCreated — unused in hands-free mode
    _ui.showToast,
    _ui.showError,
    {
      vad: true,
      stream: _stream,   // reuse the persistent mic — no per-turn re-acquire
      onTranscript: _onTranscript,
      onError: _onMicError,
      ...VAD_TUNING,
    }
  );
}

function _onMicError() {
  // Mic permission/device failure — leave conversation mode rather than spin.
  stop();
}

function _onTranscript(text) {
  if (!_enabled) return;
  const t = (text || '').trim();
  console.log('[convo] transcript:', JSON.stringify(t));
  if (!t) {
    // Heard nothing — re-arm immediately (no getUserMedia cost now).
    if (_enabled) _listen();
    return;
  }

  const input = document.getElementById('message');
  if (input) {
    input.value = t;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  _setState(STATE.THINKING);
  if (_ui.submit) {
    try { _ui.submit(); } catch (e) { console.error('convo submit failed:', e); stop(); return; }
  }
  _watchCompletion();
}

// Poll until the LLM stream AND TTS playback have both finished, then re-arm
// the mic. Enters SPEAKING (and starts the barge-in monitor) as soon as TTS
// begins so the user can interrupt.
function _watchCompletion() {
  if (_watchTimer) clearInterval(_watchTimer);
  let sawActivity = false;
  let idleTicks = 0;       // ticks spent idle before any activity was seen
  let staleStreamTicks = 0; // ticks where only a leaked _streamActive looks busy
  const MAX_IDLE_TICKS = 40; // ~8s — re-arm even if a stream never registered
  _watchTimer = setInterval(() => {
    if (!_enabled) { clearInterval(_watchTimer); _watchTimer = null; return; }

    const busy = _isStreaming() || _ttsBusy();
    if (busy) {
      sawActivity = true;
      if (_ttsBusy() && _state !== STATE.SPEAKING) {
        _setState(STATE.SPEAKING);
        _startBarge();
      }
      // Wedge guard: if the ONLY busy signal is an open streaming-TTS
      // session (_streamActive) with no audio playing, nothing queued, and
      // the LLM stream finished, the session leaked (an error path skipped
      // streamingEnd). Close it after ~2s so the loop can re-arm instead of
      // sitting in "speaking" forever.
      const t = window.aiTTSManager;
      const staleOnly = !_isStreaming() && t && t._streamActive &&
                        !t.isPlaying && !t._processing &&
                        !(t._queue && t._queue.length > 0);
      if (staleOnly) {
        if (++staleStreamTicks >= 10) {
          console.warn('[convo] streaming-TTS session leaked — force-closing');
          try { t.streamingEnd(''); } catch (e) { /* ignore */ }
        }
      } else {
        staleStreamTicks = 0;
      }
      return;
    }

    // Idle: re-arm once we've seen (and finished) a response, or after a grace
    // period if the submit never produced a visible stream (error, empty, etc).
    if (sawActivity || ++idleTicks >= MAX_IDLE_TICKS) {
      clearInterval(_watchTimer);
      _watchTimer = null;
      _stopBarge();
      if (_enabled) _listen();
    }
  }, 200);
}

// ── Barge-in ─────────────────────────────────────────────────────────────
//
// A standalone, analyser-only mic monitor (no MediaRecorder) that runs only
// while J.A.R.V.I.S is speaking. Sustained voice above a high threshold means
// the user has started talking → cut playback and start listening. It taps
// _bargeStream — a separate echo-cancelled capture acquired in start() — so
// the speaker output is removed before the level check (the raw VAD stream
// would hear the TTS and trip instantly).

function _startBarge() {
  if (!BARGE_ENABLED || _barge || !_stream) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    if (ctx.state === 'suspended') ctx.resume();
    // Tap an independent clone of the raw mic (same trick as the VAD): one
    // track feeding two consumers goes silent on the Web Audio side.
    const tapStream = (typeof _stream.clone === 'function') ? _stream.clone() : _stream;
    const src = ctx.createMediaStreamSource(tapStream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    src.connect(analyser);
    const buf = new Uint8Array(analyser.fftSize);

    // Calibration: sample the raw mic for 800ms WHILE TTS IS AUDIBLY PLAYING
    // (gated on isPlaying — an earlier wall-clock window measured pre-audio
    // silence and learned nothing). On a headset this hears ~the noise floor
    // → trigger lands just above VAD levels; on speakers it hears the echo
    // → trigger lands above the echo, degrading gracefully.
    const sustainMs = 250;     // voiced run required to count as a barge-in
    const dipMs = 150;         // inter-word dips shorter than this don't reset
    const MIN_THR = 0.005;     // matches the VAD threshold floor
    let calMs = 0;             // accumulated calibration time (isPlaying only)
    let calPeak = 0;
    let lastTick = performance.now();
    let threshold = 0;
    let voiceStart = 0;
    let lastAbove = 0;
    let raf = 0;
    let _peak = 0, _logT = performance.now();

    const tick = () => {
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / buf.length);
      const now = performance.now();
      const dt = now - lastTick;
      lastTick = now;

      if (rms > _peak) _peak = rms;
      if (now - _logT >= 1000) {
        console.log('[barge] level peak=' + _peak.toFixed(4) + ' thr=' + (threshold || 0).toFixed(4) + ' calMs=' + (calMs | 0));
        _peak = 0; _logT = now;
      }

      if (threshold === 0) {
        const tts = window.aiTTSManager;
        if (tts && tts.isPlaying) {
          calMs += dt;
          if (rms > calPeak) calPeak = rms;
          if (calMs >= 800) {
            threshold = Math.max(MIN_THR, calPeak * 1.8);
            console.log('[barge] calibrated thr=' + threshold.toFixed(4) + ' (tts-echo peak=' + calPeak.toFixed(4) + ')');
          }
        }
        raf = requestAnimationFrame(tick);
        return;
      }

      if (rms > threshold) {
        if (!voiceStart) voiceStart = now;
        lastAbove = now;
        if (now - voiceStart >= sustainMs) { console.log('[barge] triggered'); _onBargeIn(); return; }
      } else if (voiceStart && now - lastAbove > dipMs) {
        voiceStart = 0; // sustained drop — not speech, reset the run
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    // stop() references `src` so the source node isn't garbage-collected
    // mid-monitor (which would mute the analyser). The cloned tap tracks are
    // ours to stop; the shared persistent stream is never touched.
    _barge = {
      stop() {
        cancelAnimationFrame(raf);
        try { src.disconnect(); } catch (e) { /* ignore */ }
        try { ctx.close(); } catch (e) { /* ignore */ }
        if (tapStream !== _stream) {
          try { tapStream.getTracks().forEach(t => t.stop()); } catch (e) { /* ignore */ }
        }
      },
    };
  } catch (e) {
    // Web Audio unavailable — barge-in simply disabled this turn.
    _barge = null;
  }
}

function _stopBarge() {
  if (_barge) { try { _barge.stop(); } catch (e) { /* ignore */ } _barge = null; }
}

function _onBargeIn() {
  _stopBarge();
  if (_watchTimer) { clearInterval(_watchTimer); _watchTimer = null; }
  // Cut J.A.R.V.I.S off: stop TTS and abort the in-flight LLM stream.
  if (window.aiTTSManager) { try { window.aiTTSManager.stop(); } catch (e) { /* ignore */ } }
  if (window.chatModule && window.chatModule.abortCurrentRequest) {
    try { window.chatModule.abortCurrentRequest(true); } catch (e) { /* ignore */ }
  }
  if (_enabled) _listen();
}

const voiceConversationModule = {
  isActive, getState, configure, start, stop, toggle, STATE,
};

export { voiceConversationModule };
export default voiceConversationModule;
