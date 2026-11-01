#!/usr/bin/env python3
"""DUI の確認用動画の古いものを消す（標準ライブラリのみ）。

消すもの（出力フォルダの直下だけ。下の階層の動画は見ない）:
  - N 日（config.json の cleanup.days、既定 3）より古い *.mp4
  - その動画の中間ファイルのフォルダ <名前>.work/ の中のファイル（目印 .dui-work があるフォルダだけ）と、空になったフォルダ
  - 動画が無いまま N 日より古くなった <名前>.work/（作成の途中で失敗した残り）
消さないもの:
  - 名前に keep（cleanup.keep_word）を含むもの
  - 出力フォルダの keep.txt に載せたもの（1 行に 1 つ。ファイル名・名前・ワイルドカード可。# から後は注釈）
  - 目印 .dui-output が無いフォルダ（間違ったフォルダを指定した時に何も消さないため）
  - シンボリックリンク、.work/ の中の下位フォルダ（再帰的には消さない）

使い方:
  python3 cleanup.py --dry-run         消す一覧だけ出す
  python3 cleanup.py                   消す
  python3 cleanup.py --dir PATH --days 3 --config PATH
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dui_config  # noqa: E402

OUTPUT_MARK, WORK_MARK = ".dui-output", ".dui-work"


def load_keep(folder: Path, keep_file: str) -> list[str]:
    p = folder / keep_file
    if not p.exists():
        return []
    pats = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pats.append(line)
    return pats


def is_kept(stem: str, names: list[str], word: str, pats: list[str]) -> str | None:
    if word and word.lower() in stem.lower():
        return f"名前に「{word}」"
    for pat in pats:
        for n in names:
            if fnmatch.fnmatch(n, pat):
                return f"keep.txt「{pat}」"
    return None


def newest_mtime(work: Path) -> float:
    """中のファイルのいちばん新しい時刻（フォルダ自体の時刻はファイルの出し入れで変わるので使わない）。"""
    ts = [f.stat().st_mtime for f in work.iterdir() if f.is_file() and not f.is_symlink()]
    return max(ts) if ts else work.stat().st_mtime


def plan(folder: Path, days: float, word: str, pats: list[str], now: float) -> tuple[list[dict], list[dict]]:
    limit = now - days * 86400
    delete, kept = [], []
    seen_works = set()
    for mp4 in sorted(folder.glob("*.mp4")):
        if mp4.is_symlink() or not mp4.is_file():
            continue
        stem = mp4.stem
        work = folder / f"{stem}.work"
        age_d = round((now - mp4.stat().st_mtime) / 86400, 2)
        seen_works.add(work.name)
        if mp4.stat().st_mtime >= limit:
            continue
        reason = is_kept(stem, [mp4.name, stem, work.name], word, pats)
        if reason:
            kept.append({"name": mp4.name, "age_days": age_d, "reason": reason})
            continue
        delete.append({"name": mp4.name, "age_days": age_d, "mp4": mp4,
                       "work": work if (work / WORK_MARK).is_file() and not work.is_symlink() else None})
    for work in sorted(folder.glob("*.work")):
        if work.name in seen_works or work.is_symlink() or not (work / WORK_MARK).is_file():
            continue
        if newest_mtime(work) >= limit:
            continue
        stem = work.name[:-5]
        age_d = round((now - newest_mtime(work)) / 86400, 2)
        reason = is_kept(stem, [stem, work.name], word, pats)
        if reason:
            kept.append({"name": work.name, "age_days": age_d, "reason": reason})
            continue
        delete.append({"name": work.name, "age_days": age_d, "mp4": None, "work": work})
    return delete, kept


def remove_work(work: Path, dry: bool) -> tuple[list[str], list[str]]:
    """中のファイルだけ消し、空なら rmdir。下位フォルダは消さずに残す。"""
    files = sorted(f for f in work.iterdir() if (f.is_file() or f.is_symlink()) and f.name != WORK_MARK)
    left = sorted(str(f.name) for f in work.iterdir() if f.is_dir() and not f.is_symlink())
    gone = [f"{work.name}/{f.name}" for f in files]
    if not dry:
        for f in files:
            f.unlink()
        if not left:
            (work / WORK_MARK).unlink()
            work.rmdir()
    if not left:
        gone.append(f"{work.name}/")
    return gone, left


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="消す一覧だけ出す")
    ap.add_argument("--dir", help="対象フォルダ（既定: config.json の output_dir）")
    ap.add_argument("--days", type=float, help="何日より古いものを消すか（既定: config.json の cleanup.days）")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = dui_config.load(args.config)
    c = cfg.get("cleanup", {})
    folder = Path(args.dir).expanduser().resolve() if args.dir else Path(cfg["output_dir"])
    days = args.days if args.days is not None else float(c.get("days", 3))
    if days < 1:
        print(json.dumps({"ok": False, "error": "days は 1 以上"}, ensure_ascii=False))
        return 1
    if not (folder / OUTPUT_MARK).is_file():
        print(json.dumps({"ok": False, "error": f"目印 {OUTPUT_MARK} が無いので何も消さない: {folder}"}, ensure_ascii=False))
        return 1
    pats = load_keep(folder, c.get("keep_file", "keep.txt"))
    delete, kept = plan(folder, days, c.get("keep_word", "keep"), pats, time.time())
    removed, leftovers = [], []
    for d in delete:
        if d["mp4"]:
            removed.append(d["mp4"].name)
            if not args.dry_run:
                d["mp4"].unlink()
        if d["work"]:
            gone, left = remove_work(d["work"], args.dry_run)
            removed += gone
            if left:
                leftovers.append({"work": d["work"].name, "subdirs_kept": left})
    print(json.dumps({"ok": True, "dry_run": args.dry_run, "folder": str(folder), "days": days,
                      "targets": [{"name": d["name"], "age_days": d["age_days"]} for d in delete],
                      "removed" if not args.dry_run else "would_remove": removed,
                      "kept": kept, "left_subdirs": leftovers}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
