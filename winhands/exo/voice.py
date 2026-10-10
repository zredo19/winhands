"""Exo's ears and voice: microphone capture with automatic end of speech, local Whisper
speech-to-text (faster-whisper, offline) and the Windows "Pablo" voice (WinRT, offline)."""
import asyncio, os, tempfile, threading, time, wave, winsound

import numpy as np

try:  # load order matters: ctranslate2 (Whisper) after WinRT crashes with an access violation
    import ctranslate2  # noqa: F401
except ImportError:
    pass

RATE = 16000
BLOCK = 0.1  # seconds per audio block
VOCAB = "Exo, Exotype, Bloc de notas, Chrome, YouTube, Spotify, WhatsApp, Claude, Word, Excel, Descargas, Escritorio."


class Endpoint:
    """Decides when an utterance ended: speech followed by `silence` seconds of quiet. Gives up after
    `max_wait` seconds with no speech and cuts at `max_len` seconds."""

    def __init__(self, block=BLOCK, silence=1.2, threshold=0.015, max_wait=8.0, max_len=30.0):
        self.block, self.silence, self.threshold = block, silence, threshold
        self.max_wait, self.max_len = max_wait, max_len
        self.n = self.quiet = 0
        self.heard = False

    def push(self, level):
        self.n += 1
        if level > self.threshold:
            self.heard, self.quiet = True, 0
        else:
            self.quiet += 1
        t, eps = self.n * self.block, 1e-9
        return ((self.heard and self.quiet * self.block >= self.silence - eps)
                or (not self.heard and t >= self.max_wait - eps) or t >= self.max_len - eps)


class Recorder:
    """record() blocks until the utterance ends (or stop()) -> float32 mono 16 kHz audio, or None if silent."""

    def __init__(self, on_level=lambda level: None):
        self.on_level, self._stop = on_level, threading.Event()

    def stop(self):
        self._stop.set()

    def record(self, endpoint=None):
        import sounddevice as sd
        ep, chunks, self._stop = endpoint or Endpoint(), [], threading.Event()
        rate = RATE
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * BLOCK))
        except Exception:  # device refuses 16 kHz: record at its own rate, resample afterwards
            rate = int(sd.query_devices(kind="input")["default_samplerate"])
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * BLOCK))
        with stream:
            while not self._stop.is_set():
                block, _ = stream.read(int(rate * BLOCK))
                block = block[:, 0].copy()
                chunks.append(block)
                level = float(np.sqrt(np.mean(block ** 2)))
                self.on_level(level)
                if ep.push(level):
                    break
        if not chunks or not ep.heard:
            return None
        audio = np.concatenate(chunks)
        if rate != RATE:
            audio = np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio)
        return audio.astype(np.float32)


class Ears:
    """Whisper model, loaded once in the background (first load downloads it, ~460 MB for 'small')."""

    def __init__(self, model=None):
        self.name = model or os.environ.get("EXO_WHISPER", "small")
        self.model, self.ready = None, threading.Event()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(self.name, device="cpu", compute_type="int8")
        self.ready.set()

    def transcribe(self, audio):
        self.ready.wait()
        segs, _ = self.model.transcribe(audio, language="es", beam_size=1, vad_filter=True,
                                        initial_prompt=VOCAB, condition_on_previous_text=False)
        return " ".join(s.text for s in segs).strip()


class Mouth:
    """Text to speech with a lower, robotic-heroic pitch. say() returns the clip length in seconds."""

    def __init__(self, voice=None, pitch=0.75, rate=1.05):
        self.voice = voice or os.environ.get("EXO_VOICE", "Pablo")
        self.pitch, self.rate = pitch, rate
        self.path = os.path.join(tempfile.gettempdir(), "exo-voice.wav")

    async def _synth(self, text):
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer
        from winrt.windows.storage.streams import DataReader
        s = SpeechSynthesizer()
        voices = list(SpeechSynthesizer.all_voices)
        pick = (next((v for v in voices if self.voice.lower() in v.display_name.lower()), None)
                or next((v for v in voices if v.language.startswith("es")), None))
        if pick:
            s.voice = pick
        s.options.audio_pitch, s.options.speaking_rate = self.pitch, self.rate
        st = await s.synthesize_text_to_stream_async(text)
        r = DataReader(st.get_input_stream_at(0))
        await r.load_async(st.size)
        buf = bytearray(st.size)
        r.read_bytes(buf)
        return bytes(buf)

    def say(self, text):
        self.stop()
        data = asyncio.run(self._synth(text))
        with open(self.path, "wb") as f:
            f.write(data)
        with wave.open(self.path) as w:
            secs = w.getnframes() / float(w.getframerate())
        winsound.PlaySound(self.path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return secs

    @staticmethod
    def stop():
        winsound.PlaySound(None, 0)


if __name__ == "__main__":  # manual check: python -m winhands.exo.voice
    m = Mouth()
    print("speaking", round(m.say("Hola, soy Exo. Habla después del pitido."), 1), "s")
    time.sleep(3)
    ears = Ears()
    print("listening...")
    a = Recorder(on_level=lambda x: None).record()
    print("heard:", ears.transcribe(a) if a is not None else "(nothing)")
