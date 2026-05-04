/**
 * voice-recorder.js
 * 
 * Simple voice recording utility with:
 * - Start/stop recording
 * - Auto-stop on silence detection
 * - Returns audio blob ready for upload
 * - Real-time volume feedback
 */

export class VoiceRecorder {
    constructor(options = {}) {
        this.silenceThreshold = options.silenceThreshold ?? -40; // dB
        this.silenceDuration = options.silenceDuration ?? 1500; // ms
        this.onVolumeChange = options.onVolumeChange ?? null;
        this.onSilenceDetected = options.onSilenceDetected ?? null;

        this.mediaRecorder = null;
        this.audioContext = null;
        this.analyser = null;
        this.stream = null;
        this.chunks = [];
        this.isRecording = false;
        this.silenceTimer = null;
        this.volumeCheckInterval = null;
    }

    dbToLinear(db) {
        return Math.pow(10, db / 20);
    }

    getCurrentVolume() {
        if (!this.analyser) return -Infinity;

        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        this.analyser.getByteFrequencyData(dataArray);

        // RMS of frequency data
        const sum = dataArray.reduce((a, b) => a + b * b, 0);
        const rms = Math.sqrt(sum / dataArray.length);

        // Convert to dB (0-255 range to -96 to 0 dB)
        return 20 * Math.log10(rms / 255) + 10;
    }

    async start() {
        try {
            // Get microphone stream
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: false, // We control volume monitoring
                },
            });

            // Setup audio context for volume monitoring
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;
            const source = this.audioContext.createMediaStreamSource(this.stream);
            source.connect(this.analyser);

            // Setup MediaRecorder
            this.mediaRecorder = new MediaRecorder(this.stream, {
                mimeType: 'audio/webm',
            });

            this.chunks = [];
            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) {
                    this.chunks.push(e.data);
                }
            };

            this.mediaRecorder.start();
            this.isRecording = true;

            // Monitor volume for silence detection
            this.volumeCheckInterval = setInterval(() => {
                const currentVolume = this.getCurrentVolume();

                if (this.onVolumeChange) {
                    this.onVolumeChange(currentVolume);
                }

                // Silence detection
                if (currentVolume < this.silenceThreshold) {
                    if (!this.silenceTimer) {
                        this.silenceTimer = setTimeout(() => {
                            this.stop();
                            if (this.onSilenceDetected) {
                                this.onSilenceDetected();
                            }
                        }, this.silenceDuration);
                    }
                } else {
                    // Reset timer if speech detected
                    if (this.silenceTimer) {
                        clearTimeout(this.silenceTimer);
                        this.silenceTimer = null;
                    }
                }
            }, 100); // Check every 100ms
        } catch (err) {
            console.error('Error accessing microphone:', err);
            throw err;
        }
    }

    async stop() {
        if (!this.mediaRecorder || !this.isRecording) return null;

        return new Promise((resolve) => {
            this.mediaRecorder.onstop = () => {
                const audioBlob = new Blob(this.chunks, { type: 'audio/webm' });

                // Cleanup
                this.stream?.getTracks().forEach(track => track.stop());
                this.audioContext?.close();
                clearInterval(this.volumeCheckInterval);
                clearTimeout(this.silenceTimer);

                this.isRecording = false;
                resolve(audioBlob);
            };

            this.mediaRecorder.stop();
        });
    }

    cleanup() {
        if (this.isRecording) {
            this.stop();
        }
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
        }
        if (this.audioContext) {
            this.audioContext.close();
        }
        clearInterval(this.volumeCheckInterval);
        clearTimeout(this.silenceTimer);
    }
}
