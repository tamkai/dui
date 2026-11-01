# DUI — DOGA UI

**AI が動画で聞き、人は押すか話すだけ。**

CLI（文字を打つ）→ GUI（画面を押す）の次として、AI エージェントとの「読む負担」を減らすための仕組みです。
AI は判断の分かれ目で、状況と選択肢を 20〜60 秒の声つき動画にしてチャットに出します。人はボタンを押すか、音声入力で注文するだけ。AI は注文を整理して「つまりこういうこと？」を短い確認動画で聞き返し、押された選択の範囲だけ動きます。

2026 年 10〜11 月の高知の AI 合宿で、AGI Cockpit の Crew（Discord で動く AI エージェント）と一緒に作りました。

## 中身

| 場所 | 何か |
|---|---|
| `skill/` | AI エージェント用のスキル（Claude Code / AGI Cockpit 向けの `SKILL.md`）、台本の型 8 種、台本の検査 |
| `tool/make_confirm.py` | 台本 JSON から確認用動画（mp4）を作る。絵と声を並列で作り、声は文字起こしで原稿と照合 |
| `tool/cleanup.py` | 溜まった確認用動画を消す（3 日より古いもの。keep は残す） |

## 必要なもの

- macOS（Apple Silicon で確認。日本語フォントはヒラギノを使う。ほかの OS は `config.json` の `fonts` を直す）
- Python 3.10 以上、ffmpeg
- [Irodori-TTS](https://github.com/Aratako/Irodori-TTS)（ローカルで動く日本語 TTS）を導入した仮想環境
- その仮想環境に `faster-whisper` と `Pillow` を追加

```bash
# 例: Irodori-TTS を ~/irodori-tts に導入した場合
~/irodori-tts/.venv/bin/pip install faster-whisper pillow
```

モデル（Irodori-TTS-600M-v3-VoiceDesign、faster-whisper small）は初回に Hugging Face から取得されます。各モデルのライセンスは配布元を確認してください。

## 使い方

```bash
git clone https://github.com/tamkai/dui.git && cd dui
cp tool/config.example.json tool/config.json   # python と irodori_dir を自分の環境に直す

# 台本を検査（標準ライブラリだけで動く）
python3 skill/scripts/check_script.py < skill/references/example-script.json

# 確認用動画を作る（out/ に mp4 ができる）
~/irodori-tts/.venv/bin/python tool/make_confirm.py skill/references/example-script.json

# 続けて何本も作るなら、モデルを読み込んだまま常駐させる
~/irodori-tts/.venv/bin/python tool/make_confirm.py serve

# 古い確認用動画を消す（まず --dry-run）
python3 tool/cleanup.py --dry-run
```

## スキルとして入れる

`skill/` を、使っているエージェントのスキルの場所に `dui` という名前で置きます。

```bash
cp -R skill ~/.claude/skills/dui      # Claude Code の例
```

SKILL.md の中の `tool/` は、このリポジトリの `tool/` を指します。置いた場所に合わせて読み替えてください。

## 台本の形

```json
{
  "title": "締めの一枚",
  "decision": "発表の締めをどちらにするか",
  "decider": "owner",
  "scenes": [
    {"type": "title", "text": "締めの一枚", "say": "発表の締めの一枚を、一つ決めてください。"},
    {"type": "question", "choices": [
      {"emoji": "✅", "label": "数字で締める", "action": "締めを数字で言い切る形に作る"},
      {"emoji": "🔄", "label": "問いで締める", "action": "締めを会場への問いの形に作る"}
    ], "say": "数字で締めるか、問いで締めるかを押してください。"}
  ]
}
```

場面の型は `title` / `line` / `situation` / `compare` / `number` / `steps` / `flow` / `question` の 8 種。詳しくは `skill/references/script-types.md`。

## ライセンス

MIT
