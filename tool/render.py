"""DUI 台本の場面ごとの静止画（8 種の型）と、色の組み合わせの検査。

Pillow だけで描く。ブラウザや HyperFrames は使わない（確認用動画は速さを優先）。
単体で試す: python render.py script.json out_dir
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageStat

import dui_config

# ---------------------------------------------------------------- 色の検査


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (前景, 背景, 最低比, 用途)。文字は 4.5、大きな見出し・図形・ボタンの輪郭は 3.0（WCAG 2.x の基準）
PAIRS = [
    ("text", "bg", 4.5, "本文の文字 / 背景"),
    ("sub", "bg", 4.5, "補足の文字 / 背景"),
    ("accent", "bg", 3.0, "大見出し・数字・図の枠 / 背景"),
    ("text", "card", 4.5, "カードの文字 / カード"),
    ("good", "card", 4.5, "「よい」の文字 / カード"),
    ("bad", "card", 4.5, "「気になる」の文字 / カード"),
    ("button_text", "button", 4.5, "ボタンの文字 / ボタン"),
    ("button_alt_text", "button_alt", 4.5, "ボタン2の文字 / ボタン2"),
    ("button", "bg", 3.0, "ボタン / 背景"),
    ("button_alt", "bg", 3.0, "ボタン2 / 背景"),
    ("button", "button_alt", 1.4, "ボタン同士の見分け"),
]


def check_theme(theme: dict) -> list[dict]:
    """全組み合わせの比を返す。ok=False が 1 つでもあれば描かない。"""
    rows = []
    for fg, bg, need, what in PAIRS:
        if fg not in theme or bg not in theme:
            rows.append({"pair": f"{fg}/{bg}", "what": what, "ratio": 0, "need": need, "ok": False, "error": "色が未定義"})
            continue
        r = contrast(theme[fg], theme[bg])
        rows.append({"pair": f"{fg}/{bg}", "what": what, "ratio": round(r, 2), "need": need, "ok": r >= need})
    return rows


def theme_for(doc: dict, cfg: dict) -> dict:
    return {**cfg["theme"], **(doc.get("theme") or {})}


# ---------------------------------------------------------------- 描画の道具


class Painter:
    def __init__(self, cfg: dict, theme: dict):
        self.cfg, self.t = cfg, theme
        self.W, self.H = cfg["video"]["width"], cfg["video"]["height"]
        self.s = self.W / 1280
        self.fonts = {k: dui_config.first_existing(v) for k, v in cfg["fonts"].items()}
        if not self.fonts.get("bold") or not self.fonts.get("regular"):
            raise RuntimeError(f"日本語フォントが見つからない: {cfg['fonts']}")

    @lru_cache(maxsize=256)
    def font(self, kind: str, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.fonts[kind], max(8, int(size * self.s)))

    def S(self, v: float) -> int:
        return int(v * self.s)

    # 禁則（行頭に来てはいけない文字は前の行へ）
    NO_HEAD = set("、。，．）」』】〉》!?！？ー…・ァィゥェォッャュョヮヵヶぁぃぅぇぉっゃゅょゎ")

    def tokens(self, text: str) -> list[str]:
        """英数字の語は切らない。日本語は 1 字ずつ。"""
        out, cur = [], ""
        for ch in text:
            if ch.isascii() and (ch.isalnum() or ch in "._-+/#@'"):
                cur += ch
                continue
            if cur:
                out.append(cur)
                cur = ""
            out.append(ch)
        if cur:
            out.append(cur)
        return out

    def wrap(self, draw, text: str, font, max_w: int) -> list[str]:
        lines, cur = [], ""
        for tok in self.tokens(text):
            if tok == "\n":
                lines.append(cur)
                cur = ""
                continue
            if cur and draw.textlength(cur + tok, font=font) > max_w and tok not in self.NO_HEAD:
                lines.append(cur.rstrip())
                cur = tok.lstrip()
            else:
                cur += tok
        if cur:
            lines.append(cur)
        return lines or [""]

    def fit(self, draw, text: str, kind: str, max_w: int, max_lines: int, size: int, min_size: int = 22):
        """文字を max_w × max_lines に収まる最大の大きさにする。1 字だけの行や語の途中の改行も避ける。"""
        while True:
            f = self.font(kind, size)
            lines = self.wrap(draw, text, f, max_w)
            too_wide = any(draw.textlength(ln, font=f) > max_w for ln in lines)
            orphan = len(lines) > 1 and len(lines[-1].strip()) <= 1
            if (len(lines) <= max_lines and not too_wide and not orphan) or size <= min_size:
                return f, lines
            size -= 4

    def block(self, draw, lines, font, x: int, y: int, fill: str, align: str = "center", gap: float = 0.25) -> int:
        """複数行を書いて、使った高さを返す。x は align=center なら中心。"""
        asc, desc = font.getmetrics()
        lh = asc + desc
        for i, ln in enumerate(lines):
            w = draw.textlength(ln, font=font)
            xx = x - w / 2 if align == "center" else x
            draw.text((xx, y + i * lh * (1 + gap)), ln, font=font, fill=fill)
        return int(lh * (1 + gap) * len(lines) - lh * gap)

    def block_h(self, lines, font, gap: float = 0.25) -> int:
        asc, desc = font.getmetrics()
        lh = asc + desc
        return int(lh * (1 + gap) * len(lines) - lh * gap)

    def emoji(self, img: Image.Image, ch: str, cx: int, cy: int, size: int) -> bool:
        path = self.fonts.get("emoji")
        if not path:
            return False
        try:
            f = ImageFont.truetype(path, 160)  # Apple Color Emoji はビットマップの大きさが決まっている
            tile = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((110, 110), ch, font=f, embedded_color=True, anchor="mm")
            box = tile.getbbox()
            if not box:
                return False
            tile = tile.crop(box)
            k = size / max(tile.size)
            tile = tile.resize((max(1, int(tile.width * k)), max(1, int(tile.height * k))), Image.LANCZOS)
            img.alpha_composite(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)))
            return True
        except Exception:
            return False

    def heading(self, draw, text: str, y: int = 70) -> int:
        f = self.font("bold", 40)
        draw.text((self.S(100), self.S(y)), text, font=f, fill=self.t["accent"])
        return self.S(y) + self.block_h([text], f)

    def frame(self, idx: int, total: int, doc: dict):
        img = Image.new("RGBA", (self.W, self.H), self.t["bg"])
        d = ImageDraw.Draw(img)
        # 進み具合（場面の点）
        r, gap = self.S(7), self.S(26)
        x0 = self.W / 2 - gap * (total - 1) / 2
        y = self.H - self.S(34)
        for i in range(total):
            c = self.t["accent"] if i == idx else self.t["line"]
            d.ellipse((x0 + i * gap - r, y - r, x0 + i * gap + r, y + r), fill=c)
        # 左上: 題（小さく）
        if doc.get("title"):
            d.text((self.S(40), self.S(24)), str(doc["title"]), font=self.font("regular", 24), fill=self.t["sub"])
        return img, d

    def arrow(self, d, p, q, color, width):
        import math
        d.line([p, q], fill=color, width=width)
        ang = math.atan2(q[1] - p[1], q[0] - p[0])
        L = width * 4.5
        a1 = (q[0] - L * math.cos(ang - 0.45), q[1] - L * math.sin(ang - 0.45))
        a2 = (q[0] - L * math.cos(ang + 0.45), q[1] - L * math.sin(ang + 0.45))
        d.polygon([q, a1, a2], fill=color)


# ---------------------------------------------------------------- 8 種の型


def r_title(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    f, lines = P.fit(d, sc["text"], "bold", S(1100), 2, 120)
    sub_text = f"決めること：{doc.get('decision', '')}" if doc.get("decision") else ""
    fs, sub = P.fit(d, sub_text, "regular", S(1000), 2, 40) if sub_text else (None, [])
    h = P.block_h(lines, f) + S(70) + (P.block_h(sub, fs) if sub else 0)
    y = (H - h) // 2 - S(10)
    y += P.block(d, lines, f, W // 2, y, t["text"]) + S(30)
    d.rounded_rectangle((W // 2 - S(60), y, W // 2 + S(60), y + S(8)), radius=S(4), fill=t["accent"])
    if sub:
        P.block(d, sub, fs, W // 2, y + S(40), t["sub"])


def r_line(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    f, lines = P.fit(d, sc["text"], "bold", S(1100), 2, 110)
    h = P.block_h(lines, f)
    y = (H - h) // 2 - S(20)
    P.block(d, lines, f, W // 2, y, t["text"])
    w = max(d.textlength(ln, font=f) for ln in lines)
    d.rounded_rectangle((W // 2 - w / 2, y + h + S(28), W // 2 + w / 2, y + h + S(40)), radius=S(6), fill=t["accent"])


def r_situation(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    P.heading(d, sc.get("heading", "いまの状況"))
    pts = sc["points"]
    ch, gap = S(130), S(30)
    y = S(150) + (S(500) - (ch * len(pts) + gap * (len(pts) - 1))) // 2
    for p in pts:
        d.rounded_rectangle((S(100), y, W - S(100), y + ch), radius=S(18), fill=t["card"], outline=t["line"], width=S(2))
        d.rounded_rectangle((S(100), y, S(116), y + ch), radius=S(8), fill=t["accent"])
        f, lines = P.fit(d, p, "bold", W - S(300), 1, 56, 30)
        P.block(d, lines, f, S(150), y + (ch - P.block_h(lines, f)) // 2 - S(4), t["text"], align="left")
        y += ch + gap


def r_compare(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    P.heading(d, sc.get("heading", "くらべる"))
    items = sc["items"]
    n, gap = len(items), S(40)
    cw = (W - S(200) - gap * (n - 1)) // n
    top, bot = S(140), S(640)
    labels = [P.fit(d, it.get("label", ""), "bold", cw - S(60), 2, 56, 30) for it in items]
    lab_h = max(P.block_h(ls, f) for f, ls in labels)
    for i, it in enumerate(items):
        x = S(100) + i * (cw + gap)
        d.rounded_rectangle((x, top, x + cw, bot), radius=S(22), fill=t["card"], outline=t["line"], width=S(2))
        f, lines = labels[i]
        P.block(d, lines, f, x + cw // 2, top + S(36) + (lab_h - P.block_h(lines, f)) // 2, t["text"])
        y = top + S(36) + lab_h + S(26)
        d.line((x + S(30), y, x + cw - S(30), y), fill=t["line"], width=S(2))
        y += S(30)
        for key, tag, col in (("good", "よい", t["good"]), ("bad", "気になる", t["bad"])):
            if not it.get(key):
                continue
            ft = P.font("bold", 30)
            d.text((x + S(36), y), tag, font=ft, fill=col)
            y += P.block_h([tag], ft) + S(10)
            fb, bl = P.fit(d, it[key], "bold", cw - S(72), 3, 44, 26)
            y += P.block(d, bl, fb, x + S(36), y, t["text"], align="left") + S(34)


def r_number(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    fv, fu = P.font("bold", 220), P.font("bold", 80)
    v, u = str(sc["value"]), str(sc["unit"])
    while d.textlength(v, font=fv) + d.textlength(u, font=fu) > S(1100) and fv.size > 60:
        fv = ImageFont.truetype(fv.path, fv.size - 10)
    wv, wu = d.textlength(v, font=fv), d.textlength(u, font=fu)
    x = (W - wv - wu - S(16)) / 2
    base = H // 2 + S(30)
    d.text((x, base), v, font=fv, fill=t["accent"], anchor="ls")
    d.text((x + wv + S(16), base), u, font=fu, fill=t["text"], anchor="ls")
    fc, cl = P.fit(d, str(sc["caption"]), "regular", S(1000), 2, 48)
    P.block(d, cl, fc, W // 2, base + S(50), t["sub"])


def r_steps(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    P.heading(d, sc.get("heading", "手順"))
    steps = sc["steps"]
    icons = sc.get("icons") or [None] * len(steps)
    n = len(steps)
    colw = (W - S(160)) / n
    cy, r = S(340), S(56)
    for i, st in enumerate(steps):
        label = st["text"] if isinstance(st, dict) else str(st)
        icon = (st.get("icon") if isinstance(st, dict) else None) or (icons[i] if i < len(icons) else None)
        cx = S(80) + colw * (i + 0.5)
        if i < n - 1:
            P.arrow(d, (cx + r + S(14), cy), (cx + colw - r - S(14), cy), t["line"], S(6))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=t["button"])
        d.text((cx, cy), str(i + 1), font=P.font("bold", 56), fill=t["button_text"], anchor="mm")
        if icon:
            P.emoji(img, icon, int(cx), cy - r - S(60), S(72))
        f, lines = P.fit(d, label, "bold", int(colw - S(24)), 3, 40 if n <= 3 else 34, 22)
        P.block(d, lines, f, int(cx), cy + r + S(34), t["text"])


def r_flow(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    P.heading(d, sc.get("heading", "流れ"))
    nodes = [n["text"] if isinstance(n, dict) else str(n) for n in sc["nodes"]]
    n = len(nodes)
    rows = [list(range(n))] if n <= 4 else [list(range((n + 1) // 2)), list(range((n + 1) // 2, n))]
    bw = min(S(250), int((W - S(200) - S(70) * (max(len(r) for r in rows) - 1)) / max(len(r) for r in rows)))
    bh = S(150)
    rects = {}
    ys = [S(330)] if len(rows) == 1 else [S(200), S(450)]
    for row, y in zip(rows, ys):
        gap = (W - S(200) - bw * len(row)) / max(1, len(row) - 1) if len(row) > 1 else 0
        for j, k in enumerate(row):
            x = S(100) + j * (bw + gap) if len(row) > 1 else (W - bw) / 2
            rects[k] = (x, y - bh / 2, x + bw, y + bh / 2)

    def clip(a, b):  # a の中心から b の中心へ向かう線を、a の枠で切る
        ax, ay = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
        bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        dx, dy = bx - ax, by - ay
        hw, hh = (a[2] - a[0]) / 2 + S(10), (a[3] - a[1]) / 2 + S(10)
        k = min(hw / abs(dx) if dx else 1e9, hh / abs(dy) if dy else 1e9)
        return ax + dx * k, ay + dy * k

    for e in sc.get("edges") or []:
        a, b = rects[e[0]], rects[e[1]]
        P.arrow(d, clip(a, b), clip(b, a), t["sub"], S(5))
    for k, rc in rects.items():
        d.rounded_rectangle(rc, radius=S(18), fill=t["card"], outline=t["accent"], width=S(4))
        f, lines = P.fit(d, nodes[k], "bold", int(bw - S(30)), 3, 38, 20)
        h = P.block_h(lines, f)
        P.block(d, lines, f, int((rc[0] + rc[2]) / 2), int((rc[1] + rc[3]) / 2 - h / 2 - S(4)), t["text"])


def r_question(P: Painter, img, d, sc, doc):
    W, H, S, t = P.W, P.H, P.S, P.t
    P.heading(d, "選んでください", y=60)
    f, lines = P.fit(d, sc.get("text") or doc.get("decision", ""), "bold", S(1080), 2, 52, 30)
    y = S(120) + P.block(d, lines, f, S(100), S(120), t["text"], align="left") + S(36)
    ch = sc["choices"]
    n = len(ch)
    gap = S(22)
    bh = min(S(150), int((H - S(70) - y - gap * (n - 1)) / n))
    for i, c in enumerate(ch):
        bg, fg = (t["button"], t["button_text"]) if i % 2 == 0 else (t["button_alt"], t["button_alt_text"])
        x0, x1 = S(100), W - S(100)
        d.rounded_rectangle((x0, y + S(6), x1, y + bh + S(6)), radius=S(28), fill=t["line"])  # 影
        d.rounded_rectangle((x0, y, x1, y + bh), radius=S(28), fill=bg)
        ok = P.emoji(img, c.get("emoji", ""), x0 + S(80), y + bh // 2, int(bh * 0.5))
        if not ok:
            d.text((x0 + S(80), y + bh // 2), str(i + 1), font=P.font("bold", 56), fill=fg, anchor="mm")
        fl, ll = P.fit(d, c.get("label", ""), "bold", x1 - x0 - S(200), 1, 52, 28)
        act = c.get("action", "")
        fa = P.font("regular", 26)
        act_lines = P.wrap(d, "押すと：" + act, fa, x1 - x0 - S(200)) if act and bh >= S(110) else []
        if len(act_lines) > 1:
            a = act_lines[0]
            act_lines = [a[:-1] + "…"]
        h = P.block_h(ll, fl) + (S(12) + P.block_h(act_lines, fa) if act_lines else 0)
        yy = y + (bh - h) // 2 - S(4)
        yy += P.block(d, ll, fl, x0 + S(160), yy, fg, align="left")
        if act_lines:
            P.block(d, act_lines, fa, x0 + S(160), yy + S(12), fg, align="left")
        y += bh + gap


RENDERERS = {
    "title": r_title, "line": r_line, "situation": r_situation, "compare": r_compare,
    "number": r_number, "steps": r_steps, "flow": r_flow, "question": r_question,
}


def render_scene(P: Painter, doc: dict, idx: int, out: Path) -> dict:
    scenes = doc["scenes"]
    sc = scenes[idx]
    img, d = P.frame(idx, len(scenes), doc)
    RENDERERS[sc["type"]](P, img, d, sc, doc)
    rgb = img.convert("RGB")
    rgb.save(out, optimize=False, compress_level=1)
    # 描いた後の検査: 画面がほぼ一色（文字が背景に溶けた等）なら失敗
    std = sum(ImageStat.Stat(rgb.convert("L")).stddev)
    return {"scene": idx + 1, "type": sc["type"], "png": str(out), "stddev": round(std, 1), "ok": std > 8}


def render_all(doc: dict, cfg: dict, out_dir: Path) -> dict:
    theme = theme_for(doc, cfg)
    rows = check_theme(theme)
    bad = [r for r in rows if not r["ok"]]
    if bad:
        raise ValueError("色の組み合わせが基準に届かない: " + "; ".join(f"{r['what']} {r['ratio']} < {r['need']}" for r in bad))
    P = Painter(cfg, theme)
    scenes = [render_scene(P, doc, i, out_dir / f"scene-{i + 1:02d}.png") for i in range(len(doc["scenes"]))]
    return {"contrast": rows, "scenes": scenes}


if __name__ == "__main__":
    doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps(render_all(doc, dui_config.load(), out), ensure_ascii=False, indent=1))
