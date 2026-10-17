# MedBase02との接続

既存フロントエンドはReact 19・TypeScript・Viteで、`src/domain.ts` の `DataAdapter`、固定の `EXAM_SETS`、`localStorage` の学習履歴を使用します。このリポジトリではフロントのソースは変更しません。

## 対応関係

| バックエンド | フロント |
| --- | --- |
| `GET /api/v1/sets` の `sets` | `DataAdapter.listSets()` |
| `GET /api/v1/sets/{id}/questions` の `q_and_a` | `DataAdapter.getQuestions(id)` |
| セットの `pdf_url` | `DataAdapter.getPdfUrl(id)` |
| 画像の `images[].src` | 画像ビューアへ渡すURL |

最終ダウンロードは元仕様の `questions`、画面向けAPIは `q_and_a` です。画面向けでは記述式の `options` を `{}`、`answer_text` を画面の `answer` に変換します。`generated_options` は元の記述式を上書きしません。画像はフロントの公開アセットにコピーせず、APIのURLを使います。PDFのページ番号と座標は同じ統合PDFに対応します。

## 次回フロントを改修する際の手順

1. `VITE_API_BASE_URL=http://127.0.0.1:8000` を開発環境に設定します。Geminiキーはフロントに設定しません。バックエンドのCORS許可リストに実際のViteのオリジンを指定します。
2. 起動時に問題カタログを取得し、既存2セットとAPIのセットを併合します。現行 `SourceId` の固定リテラル型、`isSource`、`examSet` を動的カタログに対応させます。
3. カタログ初期化後に学習履歴を読み込みます。現在の固定セット検証のまま未知のIDを復元すると、保存済みデータを初期化してしまうためです。
4. 既存セットID `q_and_a_sample` / `exam_2025_f` と保存キー `${source_file}:${q_id}` は維持します。APIの新規セットはUUIDをsource_fileとして使用します。
5. APIの画像・PDF URLは `/api/v1/...` から始まるため、フロントの `asset()` を通さずAPIオリジンに結び付けます。404、503、読み込み中、未公開を画面で扱います。
6. 後から投入画面を作る際は、ジョブ作成→進捗ポーリング→下書きプレビュー→本人用／共有の公開操作を追加します。

APIの質問取得例（既存型との接続は上記のカタログ改修後）:

```ts
const api = import.meta.env.VITE_API_BASE_URL ?? '';
async function getQuestions(id: string) {
  const response = await fetch(`${api}/api/v1/sets/${encodeURIComponent(id)}/questions`, {
    credentials: 'include',
  });
  if (!response.ok) throw new Error('問題を読み込めませんでした');
  const json = await response.json();
  return json.q_and_a.map((q: any) => ({
    ...q,
    images: q.images?.map((image: any) => ({ ...image, src: new URL(image.src, api || location.origin).href })),
  }));
}
```

最終的に1つのサイトとして配信する際は、同一オリジンの `/api` をバックエンドへ、その他をReactのビルド成果物へルーティングします。その公開前に `current_user` を実認証へ置き換えます。現段階では全ブラウザが同じ開発ユーザーとして動作します。

学習履歴のサーバー同期・ユーザー統計・ベクトル検索は今回のAPIに含めません。既存の学習履歴とデモ統計はフロント側に残ります。
