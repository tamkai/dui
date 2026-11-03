#!/usr/bin/env python3
"""DUI 台本 JSON の検査（references/script-types.md の決まり）。標準入力から読む。"""
import json
import re
import sys

CPS = 7.0  # 見積もりの読み上げ速度（字/秒）
MAX_TOTAL, MAX_SENT = 60.0, 40
REQ = {
    "title": {"text": 15}, "line": {"text": 20}, "situation": {"points": None},
    "compare": {"items": None}, "number": {"value": None, "unit": None, "caption": None},
    "steps": {"steps": None}, "flow": {"nodes": None, "edges": None}, "question": {"choices": None},
}
VAGUE = ("対応する", "検討する", "いい感じ", "適宜")


def main() -> int:
    errs, warns = [], []
    # Windows（cp932）でも、PowerShell のパイプが付ける BOM でも読めるように、バイト列を utf-8-sig で読む
    raw = sys.stdin.buffer.read()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"error: JSON として読めない: {e}")
        return 1
    for k in ("decision", "decider", "scenes"):
        if not doc.get(k):
            errs.append(f"全体: {k} がない")
    scenes = doc.get("scenes") or []
    total = 0.0
    for i, s in enumerate(scenes):
        at = f"場面{i + 1}({s.get('type')})"
        t = s.get("type")
        if t not in REQ:
            errs.append(f"{at}: 不明な type")
            continue
        say = (s.get("say") or "").strip()
        if not say:
            errs.append(f"{at}: say がない")
        sents = [x for x in re.split(r"[。！？!?]", say) if x.strip()]
        if len(sents) > 2:
            errs.append(f"{at}: say が {len(sents)} 文（2 文まで）")
        for x in sents:
            if len(x) > MAX_SENT:
                errs.append(f"{at}: 1 文 {len(x)} 字（{MAX_SENT} 字まで）: {x[:20]}…")
        total += float(s.get("sec") or len(say) / CPS)
        for key, limit in REQ[t].items():
            v = s.get(key)
            if v in (None, "", []):
                errs.append(f"{at}: {key} がない")
            elif limit and len(str(v)) > limit:
                errs.append(f"{at}: {key} が {len(str(v))} 字（{limit} 字まで）")
        if t == "situation" and not 1 <= len(s.get("points") or []) <= 3:
            errs.append(f"{at}: points は 1〜3 個")
        if t == "compare" and not 2 <= len(s.get("items") or []) <= 3:
            errs.append(f"{at}: items は 2〜3 個")
        if t == "steps" and not 2 <= len(s.get("steps") or []) <= 5:
            errs.append(f"{at}: steps は 2〜5 個")
        if t == "flow":
            n = len(s.get("nodes") or [])
            if not 2 <= n <= 6:
                errs.append(f"{at}: nodes は 2〜6 個")
            for e in s.get("edges") or []:
                if len(e) != 2 or not all(isinstance(x, int) and 0 <= x < n for x in e):
                    errs.append(f"{at}: edges {e} が nodes の添字でない")
        if t == "question":
            ch = s.get("choices") or []
            if not 2 <= len(ch) <= 3:
                errs.append(f"{at}: choices は 2〜3 個")
            emojis = [c.get("emoji") for c in ch]
            if len(set(emojis)) != len(emojis):
                errs.append(f"{at}: emoji が重複")
            for c in ch:
                if not (c.get("emoji") and c.get("label") and c.get("action")):
                    errs.append(f"{at}: choice に emoji/label/action が揃っていない")
                elif any(v in c["action"] for v in VAGUE):
                    warns.append(f"{at}: action が曖昧: {c['action']}")
                if c.get("label") and c["label"] not in say and c.get("emoji") not in say:
                    warns.append(f"{at}: 選択肢「{c['label']}」が say で読まれていない")
    qs = [i for i, s in enumerate(scenes) if s.get("type") == "question"]
    if qs != [len(scenes) - 1] and scenes:
        errs.append("question は最後に 1 つだけ")
    if total > MAX_TOTAL:
        errs.append(f"全体: 見積もり {total:.0f} 秒（60 秒まで）")
    for e in errs:
        print("error:", e)
    for w in warns:
        print("warning:", w)
    print(f"scenes={len(scenes)} estimated={total:.0f}s errors={len(errs)} warnings={len(warns)}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
