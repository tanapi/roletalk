# RoleTalk PoC

3分間でAIとディスカッションし、話し方を診断するPoCです。

## 概要

- フロントエンド: Vue + Vite
- バックエンド: FastAPI
- 音声対話: リアルタイム音声APIとのブリッジ
- 採点: 会話ログ、音声メトリクス、採点用文字起こしを使った分析
- プロンプト本文: リポジトリには含めず、実行環境のSecretまたはローカル専用ファイルから読み込み

## ディレクトリ

- `frontend/`: 通話UI、人格選択、お題選択、会話ログ、採点表示
- `backend/app/main.py`: API、セッション管理、採点ロジック
- `backend/app/realtime_bridge.py`: 音声ブリッジ
- `backend/app/prompts.py`: プロンプト設定ローダー
- `backend/prompts.example.json`: プロンプト設定の雛形
- `.github/workflows/deploy.yml`: 手動リリース用workflow

## ローカル起動

```bash
docker compose up --build
```

ブラウザで `http://localhost:5173` を開きます。

ローカルで実際の音声経路まで確認する場合は、ブラウザ、バックエンド、外部音声サービスが相互に到達できるURL設定が必要です。具体的な公開先やアカウント情報はリポジトリに含めません。

## プロンプト管理

公開リポジトリにプロンプト本文を置かないため、`backend/app/prompts.py` はローダーと組み立て処理だけを持ちます。

- 本番: 実行環境のSecretから `ROLETALK_PROMPTS_JSON` として注入
- ローカル: `backend/prompts.local.json` を作成して `ROLETALK_PROMPTS_FILE=backend/prompts.local.json` を指定
- 公開用雛形: `backend/prompts.example.json`

`backend/prompts.local.json` は `.gitignore` 対象です。実プロンプトをGitHubへ含めないでください。

## 検証

```bash
PYTHONPYCACHEPREFIX=/private/tmp/roletalk-pycache python3 -m py_compile backend/app/main.py backend/app/prompts.py backend/app/realtime_bridge.py backend/app/voice.py backend/app/webrtc_live.py backend/app/cloudflare_realtime.py
cd frontend && npm run build
```
