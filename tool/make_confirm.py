#!/usr/bin/env python3
"""DUI の確認用動画を作る。台本 JSON → 検査 → 絵と声を並列 → ASR 照合（ずれた発話だけ作り直し）→ mp4。

使い方:
  python3 make_confirm.py script.json [--name NAME] [--config PATH]   1 本作る
  python3 make_confirm.py serve [--config PATH]                       モデルを読み込んだまま待つ（常駐）

常駐が動いていれば、1 本作るコマンドは常駐に頼む（モデルの読み込みを省ける）。動いていなければ自分で読み込む。
Pillow/torch が無い python で起動した時は、config.json の "python" で自分を起動し直す。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dui_config  # noqa: E402

WORK_MARK = ".dui-work"      # 中間ファイルのフォルダの目印（cleanup.py はこれがあるフォルダしか触らない）
OUTPUT_MARK = ".dui-output"  # 出力フォルダの目印


def sock_path(cfg: dict) -> Path:
    return Path(cfg["output_dir"]) / ".dui-server.sock"


# ---------------------------------------------------------------- 時間の記録


class Clock:
    def __init__(self):
        self.t0 = time.time()
        self.rows: list[dict] = []
        self.lock = threading.Lock()

    def span(self, name: str, start: float, end: float | None = None, **extra):
        end = end or time.time()
        with self.lock:
            self.rows.append({"stage": name, "start": round(start - self.t0, 2), "end": round(end - self.t0, 2),
                              "sec": round(end - start, 2), **extra})

    def summary(self) -> dict:
        groups: dict[str, dict] = {}
        for r in self.rows:
            key = r["stage"].split("#")[0]
            g = groups.setdefault(key, {"sec_sum": 0.0, "count": 0, "first_start": r["start"], "last_end": r["end"]})
            g["sec_sum"] = round(g["sec_sum"] + r["sec"], 2)
            g["count"] += 1
            g["first_start"] = min(g["first_start"], r["start"])
            g["last_end"] = max(g["last_end"], r["end"])
        return groups


# ---------------------------------------------------------------- 本体


def ffprobe(path: Path, entries: str, stream: str | None = None) -> str:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", entries, "-of", "csv=p=0", str(path)]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def run_check(cfg: dict, script: Path) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, cfg["check_script"]], stdin=script.open("rb"), capture_output=True, text=True)
    return p.returncode == 0, p.stdout.strip()


def encode(cfg: dict, doc: dict, pngs: list[str], wavs: list[str], durs: list[float], out: Path, crf: int) -> None:
    v = cfg["video"]
    speed = float(cfg["voice"].get("speed", 1.0))
    W, H, fps = v["width"], v["height"], v["fps"]
    n = len(pngs)
    total = sum(durs)
    accent = cfg["theme"].get("accent", "#D9480F")
    accent = (doc.get("theme") or {}).get("accent", accent)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for png, d in zip(pngs, durs):
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{d:.3f}", "-i", png]
    for wav in wavs:
        cmd += ["-i", wav]
    cmd += ["-f", "lavfi", "-i", f"color=c={accent}:s={W}x{max(4, H // 120)}:r={fps}"]
    lead = int(v.get("lead_sec", 0.25) * 1000)
    fc = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p[vc];"
    # 下端の進み具合の帯（左から伸びる）。音を聞きながら残り時間が分かる
    fc += f"[vc][{2 * n}:v]overlay=x='-w+w*t/{total:.3f}':y=H-h:shortest=1[vout];"
    for i, d in enumerate(durs):
        fc += (f"[{n + i}:a]aresample=48000,atempo={speed},adelay={lead}:all=1,"
               f"apad=whole_dur={d:.3f},atrim=0:{d:.3f}[a{i}];")
    fc += "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", str(crf),
            "-r", str(fps), "-c:a", "aac", "-b:a", "128k", "-ac", "1", "-movflags", "+faststart",
            "-t", f"{total:.3f}", str(out)]
    subprocess.run(cmd, check=True)


def make(cfg: dict, script: Path, name: str | None, voice=None) -> dict:
    """1 本作る。voice を渡せば（常駐）モデルの読み込みを省く。"""
    from voice import Voice, judge
    import render

    clk = Clock()
    outdir = Path(cfg["output_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / OUTPUT_MARK).touch()
    stem = name or f"{re.sub(r'[^0-9A-Za-z_-]+', '-', script.stem).strip('-') or 'confirm'}-{datetime.now():%Y%m%d-%H%M%S}"
    work = outdir / f"{stem}.work"
    work.mkdir(parents=True, exist_ok=True)
    (work / WORK_MARK).touch()
    mp4 = outdir / f"{stem}.mp4"

    # 1. 検査
    t = time.time()
    ok, msg = run_check(cfg, script)
    clk.span("check", t)
    if not ok:
        raise SystemExit(f"台本の検査で止めた:\n{msg}")
    doc = json.loads(script.read_text(encoding="utf-8"))
    (work / "script.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    scenes = doc["scenes"]
    # 色の組み合わせは声を作る前に検査する（同色で見えないボタン・透明な強調の事故があった）
    t = time.time()
    bad = [r for r in render.check_theme(render.theme_for(doc, cfg)) if not r["ok"]]
    clk.span("colors", t)
    if bad:
        raise SystemExit("色の組み合わせが基準に届かない（config.json の theme か台本の theme を直す）:\n" +
                         "\n".join(f"  {r['what']}（{r['pair']}）: {r['ratio']} < {r['need']}" for r in bad))
    # 英字は TTS が読み違えやすい（実測: 「Ask relay」→「ラスキー・ラシン」）。say は片仮名で書くよう知らせる
    say_warnings = [f"場面{i + 1}: say に英字「{w}」。読みを片仮名で書くと崩れにくい"
                    for i, sc in enumerate(scenes) for w in re.findall(r"[A-Za-z]{2,}", sc.get("say", ""))]
    for w in say_warnings:
        print("warning:", w, file=sys.stderr)

    # 2. 絵（別スレッド）と、モデルの読み込み（TTS と ASR を同時に）
    voice = voice or Voice(cfg)
    pool = ThreadPoolExecutor(max_workers=3)

    def do_render():
        t = time.time()
        r = render.render_all(doc, cfg, work)
        clk.span("render", t, scenes=len(scenes))
        return r

    def do_load_asr():
        t = time.time()
        s = voice.load_asr()
        clk.span("load_asr", t, cached=s == 0.0)

    f_render = pool.submit(do_render)
    f_asr_load = pool.submit(do_load_asr)
    t = time.time()
    cached = voice.load_tts() == 0.0
    clk.span("load_tts", t, cached=cached)

    # 3. 声: GPU で 1 発話ずつ作り、できた順に CPU の ASR に回す。ずれたものはその場で作り直しの列へ
    base_seed = int(cfg["voice"].get("seed", 7))
    max_retries = int(cfg["asr"].get("max_retries", 2))
    asr_pool = ThreadPoolExecutor(max_workers=1)
    results: queue.Queue = queue.Queue()
    todo = [(i, 0) for i in range(len(scenes))]
    tries: dict[int, list[dict]] = {i: [] for i in range(len(scenes))}
    # 長さの上限（発話ごと）。作り直しの時、長すぎ（余計な言葉）なら縮め、短すぎ（欠落）なら伸ばす
    caps = {i: voice.max_seconds(sc["say"]) for i, sc in enumerate(scenes)}
    outstanding = 0

    def do_asr(i: int, attempt: int, wav: Path, dur: float):
        f_asr_load.result()
        t = time.time()
        text = voice.transcribe(wav)
        clk.span(f"asr#{i + 1}.{attempt}", t)
        j = judge(scenes[i]["say"], text, cfg["asr"])
        results.put((i, {"attempt": attempt, "wav": str(wav), "audio_sec": round(dur, 2), "cap_sec": round(caps[i], 2),
                         "recognized": text, **j}))

    while todo or outstanding:
        if todo:
            i, attempt = todo.pop(0)
            wav = work / f"voice-{i + 1:02d}-{attempt}.wav"
            t = time.time()
            dur = voice.synth(scenes[i]["say"], base_seed + 1009 * attempt + i, wav, caps[i])
            clk.span(f"tts#{i + 1}.{attempt}", t, audio_sec=round(dur, 2), cap_sec=round(caps[i], 2))
            asr_pool.submit(do_asr, i, attempt, wav, dur)
            outstanding += 1
        # 結果を受け取る。次に作る発話があれば待たない。無ければ ASR の結果を待つ
        while outstanding:
            try:
                i, rec = results.get(block=not todo)
            except queue.Empty:
                break
            outstanding -= 1
            tries[i].append(rec)
            if not rec["ok"] and rec["attempt"] < max_retries:
                lo, hi = cfg["asr"].get("length_range", [0.7, 1.4])
                if rec["length"] > 1.15:
                    caps[i] = max(1.0, min(caps[i], rec["audio_sec"]) * 0.8)
                elif rec["length"] < lo or rec["tail"] < cfg["asr"].get("min_tail_ratio", 0.5):
                    caps[i] *= 1.3
                todo.insert(0, (i, rec["attempt"] + 1))

    asr_report, wavs, durs = [], [], []
    v = cfg["video"]
    speed = float(cfg["voice"].get("speed", 1.0))
    for i, sc in enumerate(scenes):
        best = max(tries[i], key=lambda r: (r["ok"], r["score"]))
        asr_report.append({"scene": i + 1, "type": sc["type"], "expected": sc["say"], "recognized": best["recognized"],
                           "ok": best["ok"], "chosen_attempt": best["attempt"], "attempts": tries[i]})
        wavs.append(best["wav"])
        d = v.get("lead_sec", 0.25) + best["audio_sec"] / speed + v.get("gap_sec", 0.45)
        if sc["type"] == "question":
            d += v.get("question_hold_sec", 1.5)
        durs.append(round(max(d, float(sc.get("sec") or 0)), 3))
    (work / "asr.json").write_text(json.dumps(asr_report, ensure_ascii=False, indent=1), encoding="utf-8")

    rend = f_render.result()
    (work / "render.json").write_text(json.dumps(rend, ensure_ascii=False, indent=1), encoding="utf-8")
    if not all(s["ok"] for s in rend["scenes"]):
        raise SystemExit(f"描いた絵がほぼ一色の場面がある: {rend['scenes']}")

    # 4. 書き出し（10MB を超えたら画質を下げてやり直す）
    crf = int(v.get("crf", 22))
    for _ in range(3):
        t = time.time()
        encode(cfg, doc, [s["png"] for s in rend["scenes"]], wavs, durs, mp4, crf)
        clk.span("encode", t, crf=crf)
        mb = mp4.stat().st_size / 1e6
        if mb <= v.get("max_mb", 9.5):
            break
        crf += 6

    pool.shutdown()
    asr_pool.shutdown()
    clk.span("total", clk.t0)
    info = {
        "mp4": str(mp4), "work": str(work), "mb": round(mp4.stat().st_size / 1e6, 2),
        "duration": float(ffprobe(mp4, "format=duration")),
        "video": ffprobe(mp4, "stream=codec_name,width,height", "v:0"),
        "audio": ffprobe(mp4, "stream=codec_name,sample_rate", "a:0"),
        "asr_all_ok": all(r["ok"] for r in asr_report),
        "asr_failed_scenes": [r["scene"] for r in asr_report if not r["ok"]],
        "say_warnings": say_warnings,
        "retries": sum(len(r["attempts"]) - 1 for r in asr_report),
        "total_sec": clk.rows[-1]["sec"],
        "stages": clk.summary(),
        "timeline": clk.rows,
    }
    (work / "timings.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def print_report(info: dict) -> None:
    print(f"mp4: {info['mp4']}  ({info['duration']:.1f}s, {info['mb']}MB, video={info['video']}, audio={info['audio']})")
    print(f"ASR 照合: {'全発話 OK' if info['asr_all_ok'] else '要確認 ' + str(info['asr_failed_scenes'])}  作り直し {info['retries']} 回")
    for w in info.get("say_warnings", []):
        print("warning:", w)
    print("工程           合計秒  回数  開始→終了(壁時計)")
    for k, g in info["stages"].items():
        print(f"  {k:<12} {g['sec_sum']:>6.2f}  {g['count']:>4}  {g['first_start']:>6.2f} → {g['last_end']:.2f}")
    print(f"合計 {info['total_sec']:.1f} 秒  記録: {Path(info['work']) / 'timings.json'}")


# ---------------------------------------------------------------- 常駐


def serve(cfg: dict) -> None:
    from multiprocessing.connection import Listener
    from voice import Voice

    voice = Voice(cfg)
    t = time.time()
    with ThreadPoolExecutor(2) as p:
        a = p.submit(voice.load_asr)
        voice.load_tts()
        a.result()
    sp = sock_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        sp.unlink()  # 前回の残り（ソケットのファイル 1 つだけ）
    key = os.urandom(16).hex()
    keyfile = sp.with_suffix(".key")
    keyfile.write_text(key)
    keyfile.chmod(0o600)
    print(f"常駐開始（モデル読み込み {time.time() - t:.1f}s）: {sp}  止めるには Ctrl-C", flush=True)
    lock = threading.Lock()
    with Listener(str(sp), family="AF_UNIX", authkey=key.encode()) as ls:
        while True:
            with ls.accept() as conn:
                req = conn.recv()
                with lock:
                    try:
                        info = make(cfg, Path(req["script"]), req.get("name"), voice=voice)
                        conn.send({"ok": True, "info": info})
                    except BaseException as e:  # SystemExit（検査で止めた）も返す
                        conn.send({"ok": False, "error": str(e)})


def ask_server(cfg: dict, script: Path, name: str | None):
    sp = sock_path(cfg)
    keyfile = sp.with_suffix(".key")
    if not sp.exists() or not keyfile.exists():
        return None
    from multiprocessing.connection import Client
    try:
        conn = Client(str(sp), family="AF_UNIX", authkey=keyfile.read_text().strip().encode())
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return None
    with conn:
        conn.send({"script": str(script.resolve()), "name": name})
        return conn.recv()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", help="台本 JSON のパス、または serve")
    ap.add_argument("--name", help="出力の名前（既定: 台本名-日時）")
    ap.add_argument("--config", help="config.json のパス（既定: 道具と同じ場所 / 環境変数 DUI_CONFIG）")
    ap.add_argument("--no-server", action="store_true", help="常駐があっても使わない")
    args = ap.parse_args()
    cfg = dui_config.load(args.config)

    if args.script != "serve" and not args.no_server:
        res = ask_server(cfg, Path(args.script), args.name)
        if res is not None:
            if not res["ok"]:
                print(res["error"], file=sys.stderr)
                return 1
            print("（常駐で作成）")
            print_report(res["info"])
            return 0

    try:
        import PIL  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        py = cfg.get("python")
        if py and Path(py).exists() and Path(sys.executable).resolve() != Path(py).resolve():
            os.execv(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]])
        print("Pillow / torch が入った python がない。config.json の python を確かめて", file=sys.stderr)
        return 1

    if args.script == "serve":
        serve(cfg)
        return 0
    info = make(cfg, Path(args.script), args.name)
    print_report(info)
    return 0 if info["asr_all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
