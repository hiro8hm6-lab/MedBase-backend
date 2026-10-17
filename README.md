# MedBase backend

試験PDFを **統合 → 問題抽出 → 画像切り出し → 解答解説生成 → 最終JSON** に変換するバックエンドです。APIと処理ワーカーを分け、PostgreSQLにジョブを保存します。フロントエンドは別リポジトリ [MedBase02](https://github.com/hiro8hm6-lab/MedBase02) を想定しています。

今回はローカル開発用です。APIは `127.0.0.1:8000` だけに公開し、認証は `DEV_USER_ID` の固定ユーザーです。将来のログインは `medbase.identity.current_user` を交換して実装します。`APP_ENV` は `local` / `test` だけを受け付けます。

## 必要なもの

- Docker Desktop（Linuxコンテナ、WindowsではWSL 2）とDocker Compose 2.24以降。
- 実PDFを処理する場合はGemini APIキー。モデルは `gemini-3.8-flash`。
- ホスト側のPythonは不要です。GitHubにはコードだけを共有し、PDFやキーは各自用意します。

[Docker DesktopのWindows導入手順](https://docs.docker.com/desktop/setup/install/windows-install/) に従ってWSLを導入し、Windowsから求められた場合は再起動してください。Docker Desktopが「Running」になり、`docker info` が成功することを確認します。

## まずAPIキーなしでDockerを確認

PowerShellの場合:

```powershell
.\scripts\start.ps1 -Mock
```

他の環境や手動起動の場合:

```sh
cp .env.example .env
docker compose -f compose.yaml -f compose.mock.yaml up -d --build --wait
docker compose exec -T api python scripts/smoke.py --directory /app/storage/smoke
```

模擬モードは4問の合成PDFだけを受け付け、記述式・連問・画像・公式正答・ZIP・確認後公開まで検証します。実際の医学問題をOCRしたり、医学的な解説を生成したりするモードではありません。結果には `llm_mode=mock` が記録されます。

APIドキュメント: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## Geminiで実PDFを処理

`.env.example` を `.env` にコピーし、次を設定します。`.env` はGit管理されません。

```dotenv
GEMINI_API_KEY=ここに自分のAPIキー
GEMINI_MODEL=gemini-3.8-flash
LLM_MODE=gemini
```

PowerShellの環境変数 `$env:GEMINI_API_KEY` から渡すこともできます。キーはバックエンドだけに渡し、フロントエンドの `VITE_` 変数には設定しません。

```powershell
.\scripts\start.ps1
```

手動の場合は `docker compose up -d --build --wait`。模擬モードから戻す場合も、追加の `compose.mock.yaml` を付けずに起動し直します。キーを変更した場合は `docker compose up -d --force-recreate api worker` で反映します。

Swagger UIの `POST /api/v1/jobs` から `author` と複数の `files` を指定します。PowerShellでは `curl` の別名を避けて `curl.exe` を使用します。

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/jobs `
  -F 'author=山田太郎' `
  -F 'files=@C:/Users/yourname/Downloads/questions.pdf;type=application/pdf' `
  -F 'files=@C:/Users/yourname/Downloads/figures.pdf;type=application/pdf' `
  -F 'files=@C:/Users/yourname/Downloads/answers.pdf;type=application/pdf'
```

作者名は前後の空白だけを除去し、複数名は `山田太郎、佐藤花子` のような1つの文字列で指定します。作者名とデータ所有者は別です。APIでは作者名必須、CLIでは `--author` が `AUTHOR_NAME` より優先します。

返されたジョブIDで `GET /api/v1/jobs/{id}` を確認します。状態は `queued`、`running`、`completed`、`completed_with_errors`、`failed`。`stage` と `progress` に工程・件数、`results` に試験ごとのセットIDが入ります。

| API（すべて `/api/v1` 以下） | 用途 |
| --- | --- |
| `POST /jobs` | PDF・PNG・JPG・HEICをアップロード。202とジョブIDを返す |
| `GET /jobs` / `GET /jobs/{id}` | 自分の履歴・進捗・エラー。履歴は `limit` / `offset` 対応 |
| `POST /jobs/{id}/retry` | 失敗または部分失敗を再実行。完了した工程・解説は再利用 |
| `GET /jobs/{id}/logs/{name}` | `input_log.json`、`classification.json`、`llm_usage.jsonl` |
| `GET /sets?include_drafts=true` | 自分の下書きも含めたセット一覧 |
| `GET /sets/{id}/result` | 正規形式の `result.json` |
| `GET /sets/{id}/download` | PDF・JSON・画像・ログのZIP |
| `POST /sets/{id}/publish` | `{"visibility":"private"}` または `{"visibility":"shared"}` で確認後に公開 |
| `GET /sets` | 公開された自分のセットと共有セット |
| `GET /sets/{id}/questions` | フロント互換の `q_and_a` データ |
| `GET /sets/{id}/pdf` | 統合PDF |
| `GET /sets/{id}/images/{q_id}/{index}` | 問題画像。indexは0始まり |

下書きは所有者だけが取得できます。本人用セットも所有者だけ、共有セットは他の利用者から読めます。エラーを含むセットと実行中のジョブは公開できません。APIは利用者IDをリクエストから受け取りません。

## 処理と成果物

入力上限は1ファイル250MiB、合計500MiBです。元ファイルの保持と解析用コピーを分け、Geminiへの入力を最大8ページ・40MBで分割します。単一ページでも大きすぎる場合は200dpiの解析用コピーを作り、原本は変更しません。

- ファイル名・表紙から試験と冊子を分類し、問題→図版→正答表の順で統合。同じ試験の共有正答表は対応区分だけを抽出します。
- 完全に同じファイル・描画内容が一致するページを除外。白紙判定は保守的に行い、不確実なページは残します。PDFのページ内容は再圧縮しません。
- 文字化けするテキスト層に依存せず、Geminiで原本の見た目を読み取ります。例題と本問を区別し、先に問題・別冊・公式正答の索引を作ります。
- 抽出は連問を崩さず、原則6問・関連ページ8ページ以内をまとめて処理します。解説は単問または連問グループ単位で生成します。
- 画像はPopplerで200dpi描画し、座標に上下左右2%の余白を付けてPNG化。連問で共有する画像も問題ごとに保存します。
- 太字、Unicode、選択肢a-l、省略項目、公式正答、記述式の3択生成は、同梱の日本語プロンプトと検証モデルで扱います。
- 解説の失敗は初回＋最大3回再試行。失敗問題は問題情報を維持し、`status=error` と理由を出力します。欠落を成功扱いにしません。
- 429や一時的なサーバー障害では待機して再試行し、成功済みの工程・解析用PDFを保持します。上限が続く場合はジョブを失敗として記録します。[AI Studioの利用制限](https://ai.google.dev/gemini-api/docs/rate-limits)を確認し、解消後にretry APIから再開できます。
- 日次上限が明示された場合は同じ処理内の後続API呼び出しを止めます。解説工程で上限に達した場合も問題情報と成功済み解説を成果物に残し、未生成問題をエラーとして保存します。利用枠の変更後はretryで再開できます。
- Geminiへ一時アップロードしたファイルは処理後に削除を試みます。ログにはAPIキーや生のAPIエラーを記録しません。

試験ごとのZIPには `result.json`、`result.schema.json`、`merged.pdf`、`clips/`、`pdf_merge_log.json`、`crop_log.json`、`merge_log.txt` と途中の問題・解説JSONを含めます。`page` は削除・回転を反映した統合PDFの物理ページ番号（1始まり）です。

仕様内の不整合は、q_idを整数、結合キーq_idだけ重複可、記述式ではoption_explanations省略、正常／失敗の別スキーマ、として統一しています。最終JSONはUTF-8・BOMなし・2スペース、生成日時は日本時間です。

## 停止・再開と保存

```sh
docker compose logs -f worker
docker compose stop
docker compose up -d
```

PostgreSQLと成果物は名前付きボリュームに残ります。`docker compose down` でも残り、`down -v` はデータを削除します。中断ジョブはリース切れ（通常120秒）後に回収し、保存済み工程から再開します。未完了のAI呼び出しは再実行されることがあります。再試行するジョブのセットは確認用の下書きに戻ります。模擬／実APIのモードはジョブに保存し、異なるモードのワーカーはそのジョブを処理しません。

## 開発・テスト

Python 3.13、uv 0.9.2、Popplerを使用します。

```sh
uv sync --frozen
uv run ruff check .
uv run pytest -q
```

PostgreSQLのロックとマイグレーションは `TEST_POSTGRES_URL` がある場合に検証します。GitHub ActionsではPostgreSQL 17を立ち上げ、この検証とDockerのHTTPスモークテスト・再起動後の永続化を実行します。APIキーはCIに不要です。

CLIから同じ処理を実行することもできます。

```sh
uv run medbase run questions.pdf figures.pdf answers.pdf --author "山田太郎" --output storage/manual-run
```

提供サンプルの実API検証は模擬テストと分けます。出力したA区分の結果は次で75問・公式正答・A075=600・画像ファイルを照合できます。

```sh
uv run medbase check-sample --result path/to/result.json --official path/to/tp240424-01seitou.pdf
```

3つの提供サンプルをAPIに送信し、完了時にZIPを取得して同じ受け入れ検証を行う場合:

```powershell
uv run python scripts/live_sample.py --directory C:/Users/yourname/Downloads --author "山田太郎"
```

ジョブIDは `work/live-sample/job-id.txt` に保存されます。監視だけを再開する場合は同じコマンドに `--job-id ジョブID` を追加します。失敗ジョブは先に `POST /api/v1/jobs/{id}/retry` で再投入します。実APIは利用料金・利用上限の対象となり、75問の受け入れ検証に成功した場合だけ `work/live-sample/acceptance.json` が作成されます。

フロントエンドとの接続方法と必要な変更は [接続メモ](docs/frontend.md) を参照してください。
