/**
 * webSpeechVoice.js
 *
 * Uses the browser's Web Speech API (SpeechRecognition) for voice input.
 * On final transcript, calls sendMessage() directly — no server-side Whisper needed.
 */

import { sendMessage, isStreaming } from './chat.js';
import { showToast } from '../utils/dom.js';

let recognition = null;
let isListening = false;
let sendDebounceTimer = null;
let accumulatedTranscript = '';

const SEND_DELAY_MS = 2000; // Wait 2s of silence before sending

export function initWebSpeechVoice() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        console.warn('[webSpeech] SpeechRecognition not supported in this browser');
        return;
    }

    recognition = new SpeechRecognition();
    recognition.continuous = true;       // keep listening through pauses
    recognition.interimResults = true;   // show partial results as user speaks
    recognition.lang = 'en-US';

    recognition.onresult = (event) => {
        let finalTranscript = '';
        let interimTranscript = '';

        for (let i = event.resultIndex; i < event.results.length; i++) {
            const result = event.results[i];
            if (result.isFinal) {
                finalTranscript += result[0].transcript;
            } else {
                interimTranscript += result[0].transcript;
            }
        }

        // Accumulate final segments
        if (finalTranscript) {
            accumulatedTranscript += finalTranscript;
        }

        // Show current state in the input box
        const input = document.getElementById('user-input');
        if (input) {
            input.value = accumulatedTranscript + interimTranscript;
        }

        // Reset the debounce timer — send after SEND_DELAY_MS of no new results
        clearTimeout(sendDebounceTimer);
        sendDebounceTimer = setTimeout(() => {
            if (accumulatedTranscript.trim()) {
                const text = accumulatedTranscript.trim();
                const input = document.getElementById('user-input');
                if (input) input.value = '';
                stopWebSpeech();
                sendMessage(text);
            }
        }, SEND_DELAY_MS);
    };

    recognition.onerror = (event) => {
        console.error('[webSpeech] error:', event.error);
        if (event.error === 'not-allowed') {
            showToast('microphone access denied');
        } else if (event.error !== 'aborted') {
            showToast(`speech error: ${event.error}`);
        }
        stopWebSpeech();
    };

    recognition.onend = () => {
        // Recognition ended (could be timeout with no speech)
        if (isListening) {
            stopWebSpeech();
        }
    };
}

export function startWebSpeech() {
    if (!recognition) {
        showToast('Web Speech API not available');
        return;
    }
    if (isStreaming || isListening) return;

    isListening = true;
    accumulatedTranscript = '';
    const btn = document.getElementById('web-speech-btn');
    if (btn) {
        btn.classList.add('recording');
        btn.textContent = '●';
    }

    // Clear input to show we're listening
    const input = document.getElementById('user-input');
    if (input) input.placeholder = 'Listening...';

    recognition.start();
}

export function stopWebSpeech() {
    isListening = false;
    clearTimeout(sendDebounceTimer);
    accumulatedTranscript = '';

    const btn = document.getElementById('web-speech-btn');
    if (btn) {
        btn.classList.remove('recording');
        btn.textContent = '🎙';
    }

    const input = document.getElementById('user-input');
    if (input) input.placeholder = 'Ask anything...';

    if (recognition) {
        try { recognition.stop(); } catch { /* already stopped */ }
    }
}

export function bindWebSpeechButton() {
    const btn = document.getElementById('web-speech-btn');
    if (!btn) return;

    btn.addEventListener('click', (e) => {
        e.preventDefault();
        if (isListening) {
            stopWebSpeech();
        } else {
            startWebSpeech();
        }
    });
}
