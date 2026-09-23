# vcp-screener (非公開)

ミネルヴィニ式トレンドテンプレート＋VCP判定の日次スクリーナー。毎営業日 7:00 JST に
GitHub Actions で自動実行し、**結果JSONだけ**を公開リポ `ikameron009/theme-flow` に送る
（＝ダッシュボードの「VCPスクリーナー」タブに表示。コードは非公開のまま）。

## 必要なシークレット（Settings → Secrets and variables → Actions）
1. **`THEMEFLOW_TOKEN`**（必須）… 公開リポ theme-flow に push するための Personal Access Token。
   - GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained token**
   - Repository access = `ikameron009/theme-flow` のみ / Permissions = **Contents: Read and write**
   - 発行した文字列を、この repo の Secret 名 `THEMEFLOW_TOKEN` に貼る。
   - ※未設定でもスクリーニングは走り履歴は貯まるが、公開サイトへは反映されない。
2. **`EDINETDB_API_KEY`**（任意）… 日本株の「増収増益」フィルタ(edinetdb)用。
   - 未設定なら日本株は「トレンドテンプレ＋VCP＋自作DV」だけで出力（成長フィルタは省略）。

## 実行
- 自動: `.github/workflows/screen.yml`（cron `0 22 * * 0-4` = 7:00 JST 月〜金）。
- 手動: Actions → "VCP screen (7:00 JST)" → Run workflow。

## 秘密情報
`service_account.json` / `edinetdb_key.txt` は **コミットしない**（.gitignore 済み）。
Google スプレッドシート出力は使わない（`export_vcp_json.py` が JSON を出す）。
