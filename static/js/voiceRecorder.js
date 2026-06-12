// static/js/voiceRecorder.js

/**
 * Voice recording with optional Speech-to-Text transcription.
 *
 * STT providers:
 *   "disabled"       — record audio as file attachment (original behavior)
 *   "browser"        — use Web Speech API for real-time transcription
 *   "local"          — send recording to server /api/stt/transcribe (Whisper)
 *   "endpoint:<id>"  — send recording to server /api/stt/transcribe (API)
 */

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingStartTime = null;
let recordingInterval = null;

// Browser STT state
let _recognition = null;
let _browserTranscript = '';

// Cached STT provider — refreshed on settings change
let _sttProvider = 'disabled';

// Per-recording options (VAD, transcript callback). Reset on each start.
let _opts = {};

// Speculative early transcription (conversation mode): at ~half the VAD
// silence window we snapshot the audio so far and start Whisper on it, so the
// decode runs DURING the remaining endpoint wait instead of after it. If the
// user resumes talking the speculation is discarded.
let _specPending = false;   // requestData() issued, waiting for the chunk
let _specPromise = null;    // in-flight speculative transcription (null on error)
let _specValid = false;     // no speech heard since the speculation started

// Voice Activity Detection state (for hands-free conversation mode). The
// analyser watches the live mic level and auto-stops the recording after a
// short trailing silence once speech has been detected.
let _vadCtx = null;
let _vadRaf = null;
let _vadStopped = false;
// Strong ref to the MediaStreamAudioSourceNode. Without this, Chrome
// garbage-collects the unreferenced source node, silently disconnecting it from
// the analyser so it reads flat silence (peak=0.000) even though ctx=running.
let _vadSrc = null;
// Cloned mic stream feeding the analyser. Chromium goes silent on the Web Audio
// side when one track feeds both a MediaRecorder and a MediaStreamSource, so the
// analyser taps an independent clone of the track instead.
let _vadStream = null;

/**
 * Fetch current STT provider from server settings
 */
async function refreshSttProvider() {
  try {
    const res = await fetch('/api/stt/stats', { credentials: 'same-origin' });
    if (res.ok) {
      const stats = await res.json();
      _sttProvider = stats.provider || 'disabled';
      // Notify the send button to update its icon
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
    }
  } catch (e) {
    console.warn('Failed to fetch STT stats:', e);
  }
}

/**
 * Format seconds as MM:SS
 */
function formatTime(seconds) {
  const mins = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = (seconds % 60).toString().padStart(2, '0');
  return `${mins}:${secs}`;
}

/**
 * Reset UI state after recording ends
 */
function _resetRecordingUI() {
  // Conversation mode can re-arm a NEW recorder before this (async) reset
  // runs for the old one — don't mark the live recording as stopped.
  if (!mediaRecorder || mediaRecorder.state !== 'recording') {
    isRecording = false;
  }
  if (recordingInterval) {
    clearInterval(recordingInterval);
    recordingInterval = null;
  }
  // Reset send button via global callback
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) {
    sendBtn.classList.remove('recording');
    // Only clear OUR state. In conversation mode the auto-submitted
    // transcript has often already set mode='streaming' (chat.js), which
    // voiceConversation._isStreaming() relies on — wiping it blinded the
    // loop's stream detection and let the next utterance abort the
    // in-flight response.
    if (sendBtn.dataset.mode === 'recording') {
      sendBtn.dataset.mode = '';
    }
  }
  if (window._updateSendBtnIcon) {
    setTimeout(window._updateSendBtnIcon, 50);
  }
}

/**
 * Start browser speech recognition alongside recording
 */
function startBrowserSTT() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;

  _browserTranscript = '';
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = false;
  _recognition.lang = '';

  _recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        _browserTranscript += event.results[i][0].transcript + ' ';
      }
    }
  };

  _recognition.onerror = (e) => {
    console.warn('Browser STT error:', e.error);
  };

  _recognition.start();
}

function stopBrowserSTT() {
  if (_recognition) {
    try { _recognition.stop(); } catch (e) { /* ignore */ }
    _recognition = null;
  }
  return _browserTranscript.trim();
}

/**
 * Send audio to server for transcription
 */
async function transcribeOnServer(audioBlob) {
  const formData = new FormData();
  formData.append('file', audioBlob, 'audio.webm');

  const res = await fetch('/api/stt/transcribe', {
    method: 'POST',
    credentials: 'same-origin',
    body: formData,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Transcription failed');
  }

  const data = await res.json();
  return data.text || '';
}

/**
 * Insert transcribed text into the chat input
 */
function insertTranscription(text, showToast) {
  if (!text) return;
  const input = document.getElementById('message');
  if (!input) return;

  const existing = input.value.trim();
  input.value = existing ? existing + ' ' + text : text;

  // Trigger auto-resize and icon update
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();

  if (showToast) showToast('Transcribed');
}

/**
 * Compute RMS amplitude (0..1) from an analyser's time-domain data.
 */
function _rms(analyser, buf) {
  analyser.getByteTimeDomainData(buf);
  let sum = 0;
  for (let i = 0; i < buf.length; i++) {
    const v = (buf[i] - 128) / 128; // center on 0
    sum += v * v;
  }
  return Math.sqrt(sum / buf.length);
}

/**
 * Start Voice Activity Detection on a live mic stream. Auto-stops the
 * recording after `silenceMs` of trailing silence once speech has been heard,
 * or after `maxMs` as a hard cap. `onAutoStop(reason)` fires exactly once.
 */
function _startVad(stream, opts, onAutoStop) {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    _vadStopped = false;
    _vadCtx = new AC();
    // getUserMedia was awaited before we got here, which breaks the user-gesture
    // chain — so the context can start 'suspended', and a suspended context's
    // analyser only ever reports silence (VAD would never fire). Resume it.
    if (_vadCtx.state === 'suspended') _vadCtx.resume();
    // Tap an independent clone so the analyser doesn't fight the MediaRecorder
    // for the same track (which makes the Web Audio side read pure silence).
    _vadStream = (typeof stream.clone === 'function') ? stream.clone() : stream;
    _vadSrc = _vadCtx.createMediaStreamSource(_vadStream);
    const analyser = _vadCtx.createAnalyser();
    analyser.fftSize = 512;
    _vadSrc.connect(analyser);
    const buf = new Uint8Array(analyser.fftSize);

    const threshold = opts.vadThreshold != null ? opts.vadThreshold : 0.015;
    const silenceMs = opts.silenceMs != null ? opts.silenceMs : 800;
    const minSpeechMs = opts.minSpeechMs != null ? opts.minSpeechMs : 250;
    const maxMs = opts.maxMs != null ? opts.maxMs : 20000;

    const startedAt = performance.now();
    let speechStart = 0;  // when the current above-threshold run began
    let lastVoice = 0;    // last time we were above threshold
    let speaking = false; // sustained speech confirmed
    // Fire the speculative-transcribe callback partway into the silence
    // window — late enough that most mid-sentence pauses have passed, early
    // enough that the decode overlaps the remaining wait.
    const specMs = Math.max(150, Math.min(silenceMs - 100, silenceMs / 2));
    let specFired = false;

    const fire = (reason) => {
      if (_vadStopped) return;
      _vadStopped = true;
      console.log('[vad] endpoint:', reason, '(ctx=' + _vadCtx.state + ')');
      onAutoStop(reason);
    };

    let _peak = 0, _logT = startedAt;
    const tick = () => {
      if (_vadStopped) return;
      const rms = _rms(analyser, buf);
      const now = performance.now();
      // Periodic level log so we can see whether the analyser hears anything.
      if (rms > _peak) _peak = rms;
      if (now - _logT >= 1000) {
        console.log('[vad] level peak=' + _peak.toFixed(3) + ' thr=' + threshold + ' speaking=' + speaking + ' ctx=' + _vadCtx.state);
        _peak = 0; _logT = now;
      }
      if (rms > threshold) {
        lastVoice = now;
        if (specFired) {
          // Speech resumed after a speculation started — it no longer covers
          // the full utterance; discard it and allow a new one later.
          specFired = false;
          if (opts.onSpecInvalid) opts.onSpecInvalid();
        }
        if (!speaking) {
          if (!speechStart) speechStart = now;
          if (now - speechStart >= minSpeechMs) {
            speaking = true;
            if (opts.onStateChange) opts.onStateChange('speech');
          }
        }
      } else if (!speaking) {
        speechStart = 0; // partial blip — reset
      }

      if (speaking && !specFired && opts.onSpeculative && (now - lastVoice) >= specMs) {
        specFired = true;
        opts.onSpeculative();
      }
      if (speaking && (now - lastVoice) >= silenceMs) return fire('silence');
      if (now - startedAt >= maxMs) return fire(speaking ? 'maxlen' : 'timeout');
      _vadRaf = requestAnimationFrame(tick);
    };
    _vadRaf = requestAnimationFrame(tick);
  } catch (e) {
    console.warn('VAD init failed:', e);
  }
}

function _stopVad() {
  _vadStopped = true;
  if (_vadRaf) {
    cancelAnimationFrame(_vadRaf);
    _vadRaf = null;
  }
  if (_vadSrc) {
    try { _vadSrc.disconnect(); } catch (e) { /* ignore */ }
    _vadSrc = null;
  }
  if (_vadCtx) {
    try { _vadCtx.close(); } catch (e) { /* ignore */ }
    _vadCtx = null;
  }
  // Stop the cloned analysis track (independent of the recording track).
  if (_vadStream && _vadStream !== mediaRecorder?.stream) {
    try { _vadStream.getTracks().forEach(t => t.stop()); } catch (e) { /* ignore */ }
  }
  _vadStream = null;
}

/**
 * Start voice recording.
 *
 * @param {Function} onFileCreated  Called with a File when STT is disabled/failed.
 * @param {Function} showToast      Toast helper.
 * @param {Function} showError      Error helper.
 * @param {Object}   [opts]         Conversation-mode options:
 *   - vad {boolean}          enable silence-based auto-stop
 *   - onTranscript {Function} called with the final transcript instead of
 *                             inserting it into the composer (hands-free mode)
 *   - onStateChange {Function} VAD state notifications ('speech')
 *   - vadThreshold/silenceMs/minSpeechMs/maxMs  VAD tuning
 */
export function startRecording(onFileCreated, showToast, showError, opts = {}) {
  _opts = opts || {};
  // Check for secure context (getUserMedia requires HTTPS or localhost)
  if (!window.isSecureContext) {
    if (showError) showError('Microphone requires HTTPS. Use a reverse proxy with SSL or access via localhost.');
    _resetRecordingUI();
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (showError) showError('Microphone not supported in this browser.');
    _resetRecordingUI();
    return;
  }

  audioChunks = [];
  _specPending = false;
  _specPromise = null;
  _specValid = false;

  const _handleMicError = (error) => {
    _stopVad();
    console.error('Microphone access error:', error);
    // In conversation mode, report via onError so the controller exits the
    // loop instead of re-listening forever on a permission failure.
    if (_opts.onError) {
      _opts.onError(error);
      _resetRecordingUI();
      return;
    }
    if (showError) {
      if (error.name === 'NotAllowedError') {
        showError('Microphone access denied. Check browser permissions.');
      } else if (error.name === 'NotFoundError') {
        showError('No microphone found.');
      } else {
        showError('Microphone error: ' + error.message);
      }
    }
    _resetRecordingUI();
  };

  // `owned` is false when the caller supplies a persistent stream (conversation
  // mode keeps one mic stream open across turns to avoid the ~100-300ms
  // getUserMedia re-acquire cost on every turn). We must not stop its tracks.
  const _onStream = (stream, owned) => {
    try {
      mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    } catch (e) {
      _handleMicError(e);
      return;
    }

    mediaRecorder.ondataavailable = event => {
      if (event.data.size > 0) {
        audioChunks.push(event.data);
      }
      if (_specPending) {
        // Chunk requested by the speculative path: everything captured so
        // far forms a decodable webm (the first chunk carries the container
        // header). Kick off Whisper now — the remaining silence window and
        // the decode run concurrently.
        _specPending = false;
        _specValid = true;
        const specBlob = new Blob(audioChunks, { type: 'audio/webm' });
        _specPromise = transcribeOnServer(specBlob).catch(() => null);
      }
    };

    // Speculative early transcription: only meaningful with VAD endpointing
    // and a server-side STT provider (browser STT is already incremental).
    const _serverStt = _sttProvider === 'local' || _sttProvider.startsWith('endpoint:');
    if (_opts.vad && _serverStt) {
      _opts.onSpeculative = () => {
        if (!mediaRecorder || mediaRecorder.state !== 'recording') return;
        _specPending = true;
        try { mediaRecorder.requestData(); } catch (e) { _specPending = false; }
      };
      _opts.onSpecInvalid = () => {
        _specPromise = null;
        _specValid = false;
      };
    }

    mediaRecorder.onstop = async () => {
      _stopVad();
      if (owned) stream.getTracks().forEach(track => track.stop());

      const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
        console.log('[rec] captured blob:', audioBlob.size, 'bytes from', audioChunks.length, 'chunks');
        // Reset the recording UI BEFORE delivering the transcript:
        // deliver() synchronously reaches handleChatSubmit, which sets
        // mode='streaming' — resetting afterwards clobbered that state.
        _resetRecordingUI();
        const provider = _sttProvider;
        // Hands-free conversation mode routes the transcript to a callback
        // instead of dropping it in the composer for the user to send.
        const onTranscript = _opts.onTranscript;
        const deliver = (text) => {
          if (onTranscript) onTranscript((text || '').trim());
          else insertTranscription(text, showToast);
        };

        if (provider === 'browser') {
          const transcript = stopBrowserSTT();
          if (transcript) {
            deliver(transcript);
          } else if (onTranscript) {
            onTranscript('');
          } else {
            if (showToast) showToast('No speech detected');
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
          }
        } else if (provider === 'local' || provider.startsWith('endpoint:')) {
          // Show "Transcribing..." feedback (skip in hands-free mode — the
          // conversation UI shows its own state).
          if (showToast && !onTranscript) showToast('Transcribing...', 5000);
          try {
            let transcript = null;
            if (_specPromise && _specValid) {
              // The speculative decode covers the whole utterance (only
              // silence followed it) and has been running since mid-window.
              transcript = await _specPromise;
              console.log('[stt] speculative transcript', transcript === null ? 'failed - falling back' : 'used');
            }
            _specPromise = null;
            _specValid = false;
            if (transcript === null || transcript === undefined) {
              transcript = await transcribeOnServer(audioBlob);
            }
            if (transcript) {
              deliver(transcript);
            } else if (onTranscript) {
              onTranscript('');
            } else {
              if (showToast) showToast('No speech detected');
            }
          } catch (e) {
            console.error('STT transcription error:', e);
            if (onTranscript) {
              onTranscript('');
            } else {
              if (showError) showError('Transcription failed: ' + e.message);
              // Fallback: attach as file
              const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
              if (onFileCreated) onFileCreated(audioFile);
            }
          }
        } else if (onTranscript) {
          onTranscript('');
        } else {
          // STT disabled — attach audio file
          const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
          if (onFileCreated) onFileCreated(audioFile);
        }
      };

      mediaRecorder.start();
      isRecording = true;
      recordingStartTime = new Date();

      // Start browser STT if that's the provider
      if (_sttProvider === 'browser') {
        startBrowserSTT();
      }

      // Hands-free VAD: auto-stop on trailing silence.
      if (_opts.vad) {
        _startVad(stream, _opts, () => {
          if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop();
        });
      } else if (showToast) {
        showToast('Recording...');
      }
  };

  // Reuse a caller-supplied persistent stream when present; otherwise acquire
  // one. Echo cancellation + noise suppression keep the mic from re-capturing
  // J.A.R.V.I.S's own TTS output (essential for barge-in in conversation mode).
  if (_opts.stream) {
    _onStream(_opts.stream, false);
    return;
  }
  navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  })
    .then(stream => _onStream(stream, true))
    .catch(_handleMicError);
}

/**
 * Stop voice recording
 */
export function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    // isRecording will be set to false in _resetRecordingUI called from onstop
  } else {
    _stopVad();
    _resetRecordingUI();
  }
}

/**
 * Check if currently recording
 */
export function getIsRecording() {
  return isRecording;
}

/**
 * Initialize recording state
 */
export function init() {
  isRecording = false;
  refreshSttProvider();
}

const voiceRecorderModule = {
  startRecording,
  stopRecording,
  getIsRecording,
  init,
  refreshSttProvider,
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(v) { _sttProvider = v; },
};

export default voiceRecorderModule;
