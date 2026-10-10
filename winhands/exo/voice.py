"""Exo's ears and voice: microphone capture with automatic end of speech, local Whisper
speech-to-text (faster-whisper, offline) and the Windows "Pablo" voice (WinRT, offline)."""
import asyncio, os, threading, time, wave

import numpy as np

def _cuda_dlls():
    """pip's nvidia-cublas-cu12 / nvidia-cudnn-cu12 ship DLLs that ctranslate2 loads lazily by name."""
    import glob, site
    for sp in site.getsitepackages():
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]


_cuda_dlls()
try:  # load order matters: ctranslate2 (Whisper) after WinRT crashes with an access violation
    import ctranslate2  # noqa: F401
except ImportError:
    ctranslate2 = None

RATE = 16000
BLOCK = 0.1  # seconds per audio block
VOCAB = ("Exo, Exotype, Discord, Bloc de notas, Chrome, YouTube, Spotify, WhatsApp, Telegram, Steam, Claude, "
         "Word, Excel, PowerPoint, Outlook, Descargas, Escritorio.")


def silero(audio, rate):
    """Speech segments (start, end) in seconds, from the Silero VAD that ships with faster-whisper."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    if rate != RATE:
        audio = np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio).astype(np.float32)
    opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=100)
    return [(s["start"] / RATE, s["end"] / RATE) for s in get_speech_timestamps(audio, opts)]


class SpeechEndpoint:
    """End of an utterance by a voice detector instead of loudness: thinking pauses and quiet speech no longer cut a
    long request. Stops `silence` seconds after the last word, after `max_wait` with no speech, or at `max_len`."""

    def __init__(self, detect=silero, rate=RATE, silence=2.5, max_wait=8.0, max_len=45.0, every=3):
        self.detect, self.rate, self.silence, self.max_wait, self.max_len = detect, rate, silence, max_wait, max_len
        self.every, self.chunks, self.k, self.samples = every, [], 0, 0
        self.heard, self.last_end = False, 0.0

    def push(self, block):
        self.chunks.append(block)
        self.k += 1
        self.samples += len(block)
        t, eps = self.samples / self.rate, 1e-9
        if t >= self.max_len - eps:
            return True
        if self.k % self.every == 0:  # the VAD looks at everything so far (~ms), a few times per second
            segs = self.detect(np.concatenate(self.chunks), self.rate)
            if segs:
                self.heard, self.last_end = True, segs[-1][1]
        if self.heard:
            return t - self.last_end >= self.silence - eps
        return t >= self.max_wait - eps


class Recorder:
    """record() blocks until the utterance ends (or stop()) -> float32 mono 16 kHz audio, or None if silent."""

    def __init__(self, on_level=lambda level: None):
        self.on_level, self._stop = on_level, threading.Event()

    def stop(self):
        self._stop.set()

    def record(self, endpoint=None):
        import sounddevice as sd
        chunks, self._stop = [], threading.Event()
        rate = RATE
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * BLOCK))
        except Exception:  # device refuses 16 kHz: record at its own rate, resample afterwards
            rate = int(sd.query_devices(kind="input")["default_samplerate"])
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * BLOCK))
        ep = endpoint or SpeechEndpoint(rate=rate)
        with stream:
            while not self._stop.is_set():
                block, _ = stream.read(int(rate * BLOCK))
                block = block[:, 0].copy()
                chunks.append(block)
                self.on_level(float(np.sqrt(np.mean(block ** 2))))
                if ep.push(block):
                    break
        if not chunks:
            return None
        audio = np.concatenate(chunks)
        if not ep.heard and not ep.detect(audio, rate):  # stopped by hand before the VAD had a look
            return None
        if rate != RATE:
            audio = np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio)
        return audio.astype(np.float32)


class Ears:
    """Whisper, loaded once in the background (the first run downloads the model). NVIDIA GPU: large-v3-turbo
    (~1.8 s per phrase on a GTX 1650 Ti); otherwise small on the CPU (~3 s). EXO_WHISPER overrides the model."""

    def __init__(self, model=None):
        self.name = model or os.environ.get("EXO_WHISPER")
        self.model, self.ready, self.device = None, threading.Event(), None
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        from faster_whisper import WhisperModel
        try:
            if not ctranslate2 or not ctranslate2.get_cuda_device_count():
                raise RuntimeError("no CUDA device")
            self.model = WhisperModel(self.name or "large-v3-turbo", device="cuda", compute_type="int8_float16")
            self.model.transcribe(np.zeros(RATE, np.float32), language="es")  # warm up (needs cuBLAS / cuDNN)
            self.device = "cuda"
        except Exception:
            self.model = WhisperModel(self.name or "small", device="cpu", compute_type="int8")
            self.device = "cpu"
        self.ready.set()

    def transcribe(self, audio):
        self.ready.wait()
        segs, _ = self.model.transcribe(audio, language="es", beam_size=5, vad_filter=True,
                                        initial_prompt=VOCAB, condition_on_previous_text=False)
        return " ".join(s.text for s in segs).strip()


VOICES = {  # key -> (engine, engine voice, label); Kokoro voices sound human, "pablo" is the built-in Windows voice
    "alex": ("kokoro", "em_alex", "Alex"),
    "dora": ("kokoro", "ef_dora", "Dora"),
    "santa": ("kokoro", "em_santa", "Santa"),
    "pablo": ("windows", "Pablo", "Pablo (Windows)"),
}
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def sentences(text, limit=220):
    """Split a reply into speakable chunks (so the first one plays while the rest is synthesized)."""
    import re
    out = []
    for part in re.split(r"(?<=[.!?…])\s+", text):
        part = part.strip()
        while len(part) > limit:  # a long stretch without a full stop: cut at the last comma or space
            cut = max(part.rfind(",", 0, limit), part.rfind(" ", 0, limit))
            cut = cut if cut > 0 else limit
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            out.append(part)
    return out


def kokoro_dir():
    from .brain import HOME
    return HOME / "models" / "kokoro"


def ensure_kokoro():
    """Download the Kokoro model once (~340 MB) into ~/.exo/models/kokoro. Returns the folder or None."""
    import urllib.request
    d = kokoro_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        for f in KOKORO_FILES:
            if not (d / f).exists():
                urllib.request.urlretrieve(KOKORO_URL + f, d / (f + ".part"))
                (d / (f + ".part")).replace(d / f)
        return d
    except Exception:
        return None


class Mouth:
    """Text to speech, sentence by sentence: Kokoro (local, human-sounding) or the Windows voice as a fallback.
    say() returns at once; busy() tells whether it is still talking; stop() cuts it."""

    def __init__(self, voice=None, speed=1.05):
        self.voice = voice or os.environ.get("EXO_VOICE", "alex")
        self.speed, self.gen, self._thread, self._kokoro = speed, 0, None, None
        self._lock = threading.Lock()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        d = ensure_kokoro()
        if d is None:
            return
        try:
            from kokoro_onnx import Kokoro
            with self._lock:
                self._kokoro = Kokoro(str(d / KOKORO_FILES[0]), str(d / KOKORO_FILES[1]))
        except Exception:
            self._kokoro = None

    def _synth(self, text):
        """-> (float32 samples, sample rate)."""
        engine, name, _ = VOICES.get(self.voice, VOICES["alex"])
        if engine == "kokoro" and self._kokoro is not None:
            with self._lock:
                samples, sr = self._kokoro.create(text, voice=name, speed=self.speed, lang="es")
            return np.asarray(samples, np.float32), sr
        return self._windows(text, name if engine == "windows" else "Pablo")

    @staticmethod
    def _windows(text, name):
        async def go():
            from winrt.windows.media.speechsynthesis import SpeechSynthesizer
            from winrt.windows.storage.streams import DataReader
            s = SpeechSynthesizer()
            voices = list(SpeechSynthesizer.all_voices)
            pick = (next((v for v in voices if name.lower() in v.display_name.lower()), None)
                    or next((v for v in voices if v.language.startswith("es")), None))
            if pick:
                s.voice = pick
            st = await s.synthesize_text_to_stream_async(text)
            r = DataReader(st.get_input_stream_at(0))
            await r.load_async(st.size)
            buf = bytearray(st.size)
            r.read_bytes(buf)
            return bytes(buf)
        import io
        with wave.open(io.BytesIO(asyncio.run(go()))) as w:
            sr, raw = w.getframerate(), w.readframes(w.getnframes())
        return np.frombuffer(raw, np.int16).astype(np.float32) / 32768, sr

    def say(self, text):
        self.stop()
        gen = self.gen
        self._thread = threading.Thread(target=self._run, args=(sentences(text), gen), daemon=True)
        self._thread.start()

    def _run(self, parts, gen):
        import sounddevice as sd
        if not parts:
            return
        nxt = self._synth(parts[0])
        for i in range(len(parts)):
            if gen != self.gen:
                return
            audio, sr = nxt
            sd.play(audio, sr)
            nxt = self._synth(parts[i + 1]) if i + 1 < len(parts) else None  # next one while this one plays
            end = time.time() + len(audio) / sr
            while time.time() < end:
                if gen != self.gen:
                    sd.stop()
                    return
                time.sleep(0.03)

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self.gen += 1
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass


if __name__ == "__main__":  # manual check: python -m winhands.exo.voice
    m = Mouth()
    time.sleep(2)
    m.say("Hola, soy Exo. Habla después del pitido.")
    while m.busy():
        time.sleep(0.1)
    ears = Ears()
    print("listening...")
    a = Recorder(on_level=lambda x: None).record()
    print("heard:", ears.transcribe(a) if a is not None else "(nothing)")
