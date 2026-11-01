"""声（Irodori-TTS）と文字起こし（faster-whisper）。モデルは 1 回だけ読み、使い回す。

- 新しいモデルは落とさない（HF_HUB_OFFLINE=1 と local_files_only=True）
- 長文の一括生成はしない。台本の say（1〜2 文）ごとに作る
"""
from __future__ import annotations

import difflib
import os
import re
import sys
import time
import unicodedata
import warnings
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")


class Voice:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.v, self.a = cfg["voice"], cfg["asr"]
        self.tts = None
        self.whisper = None
        if cfg.get("irodori_dir") and cfg["irodori_dir"] not in sys.path:
            sys.path.insert(0, cfg["irodori_dir"])

    def load_tts(self) -> float:
        if self.tts is not None:
            return 0.0
        t = time.time()
        from huggingface_hub import hf_hub_download
        from irodori_tts.inference_runtime import InferenceRuntime, RuntimeKey, default_runtime_device

        ck = hf_hub_download(self.v["checkpoint_repo"], "model.safetensors", local_files_only=True)
        self.tts = InferenceRuntime.from_key(
            RuntimeKey(checkpoint=ck, model_device=default_runtime_device(), codec_device=self.v.get("codec_device", "cpu"))
        )
        return time.time() - t

    def load_asr(self) -> float:
        if self.whisper is not None:
            return 0.0
        t = time.time()
        from faster_whisper import WhisperModel

        self.whisper = WhisperModel(self.a["model"], device="cpu", compute_type="int8",
                                    cpu_threads=self.a.get("cpu_threads", 6), local_files_only=True)
        return time.time() - t

    def max_seconds(self, text: str) -> float:
        """文字数から長さの上限を見積もる。短い文で予測の長さが伸びすぎ、余った時間に繰り返しや
        意味のない言葉が入る崩れがあった（9 字に 4.1 秒 →「…40秒です ミヤオ戦は星も」）。"""
        c = self.v.get("max_sec", {"base": 0.6, "per_char": 0.22, "per_pause": 0.25})
        body = re.sub(r"[、。，．,.！？!?…\s]", "", text)
        pauses = len(re.findall(r"[、。，．,.！？!?…]", text))
        return c["base"] + c["per_char"] * len(body) + c["per_pause"] * pauses

    def synth(self, text: str, seed: int, out: Path, max_sec: float | None = None) -> float:
        """1 発話を作って wav に書く。戻り値は音声の秒数（速度調整前）。"""
        from irodori_tts.inference_runtime import SamplingRequest, save_wav

        ref = self.v.get("ref_wav")
        req = SamplingRequest(text=text, caption=self.v.get("caption"), ref_wav=ref, no_ref=not ref,
                              num_steps=int(self.v.get("num_steps", 32)), seed=seed,
                              max_seconds=max(1.0, max_sec) if max_sec else 30.0)
        r = self.tts.synthesize(req)
        save_wav(out, r.audio, r.sample_rate)
        return r.audio.shape[-1] / r.sample_rate

    def transcribe(self, path: Path) -> str:
        segs, _ = self.whisper.transcribe(str(path), language="ja", beam_size=3, vad_filter=True)
        return "".join(s.text for s in segs).strip()


# ---------------------------------------------------------------- 原稿との照合

_KANSUJI = str.maketrans("〇一二三四五六七八九", "0123456789")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)  # ひらがな→カタカナ
    s = s.translate(_KANSUJI)
    return re.sub(r"[^\w]", "", s)


def judge(expected: str, recognized: str, a: dict) -> dict:
    """ASR は同音の漢字・数字を書き換えるので、完全一致は求めない。
    崩れ方の実績（後半の欠落・余計な言葉）を拾うため、全体の近さ・長さの比・末尾の近さの 3 つを見る。"""
    e, r = norm(expected), norm(recognized)
    ratio = difflib.SequenceMatcher(None, e, r).ratio() if e and r else 0.0
    k = min(6, len(e))
    tail = difflib.SequenceMatcher(None, e[-k:], r[-(k + 2):]).ratio() if k and r else 0.0
    length = len(r) / len(e) if e else 0.0
    lo, hi = a.get("length_range", [0.7, 1.4])
    ok = ratio >= a.get("min_ratio", 0.6) and tail >= a.get("min_tail_ratio", 0.5) and lo <= length <= hi
    return {"ratio": round(ratio, 3), "tail": round(tail, 3), "length": round(length, 3), "ok": ok,
            "score": round(ratio + 0.5 * tail - abs(1 - length), 3)}
