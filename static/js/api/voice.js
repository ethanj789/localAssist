import { API } from '../constants.js';
import { escHtml, setStatus, showToast } from '../utils/dom.js';
import { appendMsg } from '../ui/chatRenderer.js';
import { isStreaming, setIsStreaming, handleStreamEvent } from './chat.js';
import { VoiceRecorder } from '../utils/voiceRecorder.js';

let voiceRecorder = null;
let isRecording = false;

export function initVoiceRecorder() {
    voiceRecorder = new VoiceRecorder({
        silenceThreshold: -20,
        silenceDuration: 750,
        onVolumeChange: (db) => {
            const btn = document.getElementById('voice-btn');
            if (btn) {
                const normalized = Math.max(0, Math.min(1, (db + 50) / 50));
                btn.style.setProperty('--volume', normalized);
            }
        },
        onSilenceDetected: () => {
            console.log('Silence detected, stopping recording...');
            stopVoiceRecording();
        },
    });
}

export async function startVoiceRecording() {
    if (isStreaming || isRecording) return;

    try {
        isRecording = true;
        const btn = document.getElementById('voice-btn');
        if (btn) {
            btn.classList.add('recording');
            btn.textContent = '● stop';
        }

        await voiceRecorder.start();
    } catch (err) {
        console.error('Failed to start recording:', err);
        showToast('microphone access denied');
        isRecording = false;
        const btn = document.getElementById('voice-btn');
        if (btn) btn.classList.remove('recording');
    }
}

export async function stopVoiceRecording() {
    if (!isRecording) return;

    isRecording = false;
    const btn = document.getElementById('voice-btn');
    if (btn) {
        btn.classList.remove('recording');
        btn.textContent = '🎤 voice';
        btn.disabled = true;
    }

    try {
        const audioBlob = await voiceRecorder.stop();
        await sendVoiceMessage(audioBlob);
    } catch (err) {
        console.error('Error stopping recording:', err);
        showToast('error processing audio');
    } finally {
        if (btn) btn.disabled = false;
    }
}

export async function sendVoiceMessage(audioBlob) {
    if (isStreaming) return;

    setIsStreaming(true);
    setStatus(true, 'transcribing...');
    const sendBtn = document.getElementById('send-btn');
    if (sendBtn) sendBtn.disabled = true;

    const userDiv = appendMsg('user', '(transcribing...)');

    const formData = new FormData();
    formData.append('audio', audioBlob, 'audio.webm');

    const aId = 'msg-' + Date.now();
    const aDiv = appendMsg('assistant', '', aId);
    const aBody = aDiv.querySelector('.msg-body');
    const mdDiv = document.createElement('div');
    mdDiv.className = 'md-content';
    let cursor = document.createElement('span');
    cursor.className = 'cursor';
    aBody.appendChild(mdDiv);
    aBody.appendChild(cursor);

    const state = {
        aDiv,
        mdDiv,
        fullText: '',
        currentPill: null,
        cursor,
        linkCards: [],
    };

    try {
        const resp = await fetch(`${API}/voice-chat`, {
            method: 'POST',
            body: formData,
        });

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });

            const lines = buf.split('\n\n');
            buf = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;

                let parsed;
                try {
                    parsed = JSON.parse(line.slice(6));
                } catch {
                    continue;
                }

                const { event, data } = parsed;

                if (event === 'transcription') {
                    const userBody = userDiv.querySelector('.msg-body');
                    userBody.textContent = escHtml(data);
                    setStatus(true, 'thinking...');
                    continue;
                }

                handleStreamEvent(event, data, state);
            }
        }
    } catch (e) {
        aBody.innerHTML = `<span style="color:var(--warn)">error: ${e.message}</span>`;
        setStatus(false, 'error');
    } finally {
        cursor.remove();
        setIsStreaming(false);
        if (sendBtn) sendBtn.disabled = false;
        setStatus(true, 'ready');
    }
}

export function bindVoiceButton() {
    const voiceBtn = document.getElementById('voice-btn');
    if (!voiceBtn) return;

    voiceBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (isRecording) {
            stopVoiceRecording();
        } else {
            startVoiceRecording();
        }
    });
}
