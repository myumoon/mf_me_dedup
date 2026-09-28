# 導入手順

## 前提

- MoneyForward ME のプレミアムプランを利用し、「楽天カード」と「楽天市場(my Rakuten)」を連携する。
- MoneyForward ME の2段階認証を認証アプリ方式にする。
- Python 3.12 と uv を用意する。

## 2段階認証と1Password

1. MoneyForward ME のアカウント設定で、2段階認証を認証アプリ方式に切り替える。
2. 表示された QR コードを1Passwordで読み取り、MF ME 用のログイン項目にワンタイムパスワードを登録する。初回確認コードを入力して設定を完了する。
3. 1Password に保存した同じ TOTP 秘密鍵を、実行環境の `MF_TOTP_SECRET` として登録する。

## GitHub Actions で実行する

明細の内容は Job Summary と、エラー時に保存するスクリーンショットに含まれる。以下のワークフローは**非公開の実行用リポジトリ**だけに置く。公開リポジトリにはワークフロー、秘密情報、実データを置かない。

1. 非公開リポジトリ `mf_me_dedup_runner` を作成する。
2. `Settings > Secrets and variables > Actions` から Repository secrets を追加する。
   - `MF_EMAIL`: MoneyForward ME のメールアドレス
   - `MF_PASSWORD`: MoneyForward ME のパスワード
   - `MF_TOTP_SECRET`: 1Password に登録したものと同じ TOTP 秘密鍵
3. 次の内容を `.github/workflows/run.yml` にコピーする。`<owner>` と `<SHA>` は実行用リポジトリや各 Action の実際の値に置き換える。公開リポジトリの `ref` にはタグでなくコミット SHA を指定する。

```yaml
name: mf_me_dedup
on:
  schedule: [{ cron: "0 22 * * *" }]   # 07:00 JST
  workflow_dispatch:
    inputs:
      args: { description: "mf_me_dedup args", default: "--dry-run" }
permissions: { contents: read }
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@<SHA>
        with: { repository: <owner>/mf_me_dedup, ref: <commit SHA> }
      - uses: astral-sh/setup-uv@<SHA>
      - run: uv sync --frozen && uv run playwright install --with-deps chromium
      - run: uv run python -m mf_me_dedup $ARGS
        env:
          ARGS: ${{ inputs.args }}
          MF_EMAIL: ${{ secrets.MF_EMAIL }}
          MF_PASSWORD: ${{ secrets.MF_PASSWORD }}
          MF_TOTP_SECRET: ${{ secrets.MF_TOTP_SECRET }}
      - if: failure()
        uses: actions/upload-artifact@<SHA>
        with: { name: error, path: artifacts/, retention-days: 3 }
```

`inputs.args` は手動実行時に使われ、schedule 実行時は空になるため通常実行となる。入力値は `run:` に直接埋め込まず、環境変数を介して渡す。

## ローカルで実行する

リポジトリのルートで依存関係と Chromium を準備する。

```powershell
uv sync --frozen
uv run playwright install chromium
```

ルートに `.env` を作り、次の値を設定する。

```dotenv
MF_EMAIL=your-email@example.com
MF_PASSWORD=your-password
MF_TOTP_SECRET=your-totp-secret
```

`.env` は平文のパスワードと TOTP 秘密鍵を含み、実質的に1要素認証になる。同期フォルダには置かず、自分以外が読めない場所に保管する。

まず変更なしで結果を確認する。

```powershell
uv run --env-file .env python -m mf_me_dedup --dry-run
```

## Windows タスクスケジューラで毎日実行する

GitHub Actions からログインできない場合は、Windows 上でタスクを作り、毎日1回実行する。

1. トリガーを毎日 07:00 に設定する。
2. 操作の「プログラム/スクリプト」に uv の `uv.exe` のパスを設定する。
3. 引数に `run --env-file .env python -m mf_me_dedup` を設定する。
4. 「開始 (オプション)」にリポジトリのルートディレクトリを設定する。

## 振替を元に戻す手順

PR2（フェーズ0の実測後）に追記する
