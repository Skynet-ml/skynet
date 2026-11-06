Skynet HTTP API を使うと、スクリプトやツールから Web アプリと同じ操作ができます。実行を開始し、見守り、結果を読み取れます。このページでは、リクエストへの署名方法、完全なリファレンスの場所、そして完全な例を 1 つ紹介します。

> [!TIP]
> Claude Code、Codex、Cursor などの AI エージェントをお使いですか？ その場合は、代わりに [MCP](/docs/mcp) で接続してください。エージェントが実行ごとに支出上限を設けたうえで、あなたの代わりに実行を開始し、見守ります。

## ベース URL

すべてのリクエストを次の宛先に送信します。

```text
https://api.skynetml.com
```

## 認証

API は API トークンを使います。

1. Skynet で **設定** を開き、**API** タブを開きます。
2. **トークンを生成** をクリックします。
3. トークンはすぐにコピーしてください。Skynet がトークン全体を表示するのは 1 回だけです。

すべてのリクエストの `Authorization` ヘッダーでトークンを送信します。

```http
Authorization: Bearer skyd_...
```

- トークンは `skyd_` で始まります。
- 各ユーザーが持てる有効なトークンは最大 1 つです。新しいトークンを生成すると、古いトークンは無効になります。
- トークンを無効にするには、**トークンを無効化** をクリックします。

> [!WARNING]
> トークンはパスワードと同じように扱ってください。ソースコードやログに含めないでください。Skynet がトークンをどのように保管しているかは、[セキュリティ](/docs/security) をお読みください。

## インタラクティブリファレンス

完全なリファレンスは [api.skynetml.com/scalar](https://api.skynetml.com/scalar) にあります。各公開エンドポイント、そのフィールド、レスポンスが一覧になっています。このページからテストリクエストを送信することもできます。

リファレンスは `https://api.skynetml.com/openapi.public.json` にある OpenAPI ドキュメントを読み込んでいます。このファイルを使って、お使いの言語のクライアントを生成できます。

## 主なエンドポイント

| メソッドとパス                                  | 機能                                                        |
| ----------------------------------------------- | ----------------------------------------------------------- |
| `GET /models`                                   | 使用できるモデルを一覧表示します。                          |
| `GET /optimizers`                               | 設定に合うオプティマイザーを一覧表示します。                |
| `GET /blackbox/engines`                         | ネイティブエンジンと、それぞれが利用可能かを一覧表示します。 |
| `POST /optimize` | DSPy プログラムの実行を開始します。 |
| `POST /blackbox/run`                            | GitHub リポジトリに対する実行を開始します。                 |
| `GET /optimizations`                            | 実行を一覧表示します。                                      |
| `GET /optimizations/{optimization_id}`          | 1 つの実行のステータス、進捗、ログ、結果を返します。        |
| `GET /optimizations/{optimization_id}/artifact` | 実行で見つかった最良のバージョンを返します。                |
| `POST /optimizations/{optimization_id}/cancel`  | 実行を停止します。                                          |
| `POST /serve/{optimization_id}`                 | 新しい入力に対して最良のバージョンを実行します。            |
| `GET /health`                                   | サービスが稼働しているかを示します。                        |

## 実行を開始する

`POST /optimize` は送信ウィザードと同じ設定を受け取ります。例として、サポートメッセージをラベルに分類する DSPy プログラムを改善します。

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

重要なフィールド:

- `task.artifact.kind` は `dspy_program` です。
- `data.mode` は `pairs`（入力と期待される出力の組）または `single`（ケースがちょうど 1 つ）です。
- `optimizer.choice` は `auto`、または `GET /optimizers` が返すオプティマイザー名の 1 つです。
- `max_cost_cents` はこの実行の支出上限で、単位は米セントです。
- `is_private` の既定値は `false` です。実行を非公開にするには `true` に設定します。

`run_code` と `metric_code` のルールについては、[メトリクスとスコアラー](/docs/concepts/scorers) をお読みください。

### レスポンス

| ステータス | 意味                                                                                                                                         |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`      | Skynet が実行をキューに追加しました。本文には `optimization_id` と `status` が含まれます。                                                   |
| `202`      | 設定チェックがまだ実行中です。`Retry-After` ヘッダーの秒数だけ待ってから、同じ `Idempotency-Key` で同じ本文を送信してください。             |
| `402`      | 残高が不足しています。                                                                                                                       |
| `409`      | クォータに達したか、設定チェックが古くなっています。                                                                                         |
| `422`      | リクエストまたはワークスペースのコードがオプティマイザーに適合しません。                                                                     |

> [!NOTE]
> 必ず `Idempotency-Key` ヘッダーを送信してください。同じキーで同じリクエストを再送すると、Skynet は最初の実行を返します。2 つ目の実行は開始しません。

## 実行を見守る

ステータスが最終状態になるまで、実行をポーリングします。

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

`status` フィールドは次のいずれかです。

| ステータス                         | 最終状態 |
| ---------------------------------- | -------- |
| `pending`, `validating`, `running` | いいえ   |
| `paused`                           | いいえ   |
| `success`                          | はい     |
| `failed`, `cancelled`, `stopped`   | はい     |

ポーリングは 10〜30 秒ごとに行ってください。実行には数分から数時間かかることがあります。

実行レスポンスのすべてのフィールドは、[API リファレンス](https://api.skynetml.com/scalar) に記載されています。

## 結果を使う

ステータスが `success` になったら、最良のバージョンを取得します。

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

新しい入力に対して最良のバージョンを実行するには、`POST /serve/{optimization_id}` を送信します。各スコアの意味については、[結果](/docs/guides/results) をお読みください。

## 制限

API は Web アプリと同じクォータ、レート制限、請求を使います。[制限](/docs/limits) と [請求](/docs/concepts/billing) をお読みください。
