Skynet HTTP API 等腳本同工具可以做到網頁應用程式做到嘅嘢：開始運行、觀察運行，同埋讀取結果。呢頁講點樣幫請求簽名、完整參考喺邊，仲有一個完整例子。

> [!TIP]
> 你有冇用緊 Claude Code、Codex 或者 Cursor 呢類 AI agent？可以改為經 [MCP](/docs/mcp) 連接。咁 agent 就會幫你開始同觀察運行，而每次運行都有花費上限。

## 基本 URL

每個請求都傳送去：

```text
https://api.skynetml.com
```

## 驗證身份

API 用 API Token。

1. 喺 Skynet 打開 **設定**，再打開 **API** 分頁。
2. 撳 **產生 Token**。
3. 即刻複製個 Token。Skynet 只會顯示完整 Token 一次。

每個請求都要喺 `Authorization` 標頭傳送個 Token：

```http
Authorization: Bearer skyd_...
```

- Token 以 `skyd_` 開頭。
- 每個用戶最多只有一個有效 Token。新 Token 會令舊 Token 失效。
- 撳 **撤銷 Token** 就可以停用一個 Token。

> [!WARNING]
> 要好似對待密碼咁對待個 Token。唔好將佢放入原始碼或者日誌。想知 Skynet 點樣儲存 Token，請睇 [安全](/docs/security)。

## 互動參考

完整參考喺 [api.skynetml.com/scalar](https://api.skynetml.com/scalar)。佢列出每個公開端點、佢嘅欄位同回應。你可以喺嗰頁傳送測試請求。

呢份參考讀取 `https://api.skynetml.com/openapi.public.json` 嘅 OpenAPI 文件。你可以用呢個檔案，用你嘅程式語言產生一個用戶端。

## 主要端點

| 方法同路徑                                      | 用途                                                   |
| ----------------------------------------------- | ------------------------------------------------------ |
| `GET /models`                                   | 列出你可以用嘅模型。                                   |
| `GET /optimizers`                               | 列出適合某個設定嘅優化器。                             |
| `GET /blackbox/engines`                         | 列出原生引擎，同埋每個引擎係咪可用。                   |
| `POST /optimize` | 為 DSPy 程式開始一次運行。 |
| `POST /blackbox/run`                            | 喺一個 GitHub 儲存庫上開始一次運行。                   |
| `GET /optimizations`                            | 列出你嘅運行。                                         |
| `GET /optimizations/{optimization_id}`          | 回傳一次運行：狀態、進度、日誌同結果。                 |
| `GET /optimizations/{optimization_id}/artifact` | 回傳運行搵到嘅最佳版本。                               |
| `POST /optimizations/{optimization_id}/cancel`  | 停止一次運行。                                         |
| `POST /serve/{optimization_id}`                 | 用最佳版本處理一個新輸入。                             |
| `GET /health`                                   | 顯示服務係咪正常運作。                                 |

## 開始一次運行

`POST /optimize` 接受同提交精靈一樣嘅設定。例子會改良一個將客服訊息分類做唔同標籤嘅 DSPy 程式。

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

重要欄位：

- `task.artifact.kind` 係 `dspy_program`。
- `data.mode` 係 `pairs`（輸入連埋預期輸出）或者 `single`（啱啱好一個案例）。
- `optimizer.choice` 係 `auto`，或者 `GET /optimizers` 入面其中一個優化器名稱。
- `max_cost_cents` 係呢次運行嘅花費上限，單位係美仙。
- `is_private` 預設係 `false`。設做 `true` 就可以將運行保持私人。

`run_code` 同 `metric_code` 嘅規則，請睇 [指標同評分器](/docs/concepts/scorers)。

### 回應

| 狀態  | 意思                                                                                                         |
| ----- | ------------------------------------------------------------------------------------------------------------ |
| `201` | Skynet 已經將運行排入佇列。回應內容有 `optimization_id` 同 `status`。                                        |
| `202` | 設定檢查仲做緊。等 `Retry-After` 標頭指定嘅秒數，然後用同一個 `Idempotency-Key` 再傳送同一個內容。          |
| `402` | 你嘅結餘唔夠。                                                                                               |
| `409` | 你去到配額上限，或者設定檢查已經過時。                                                                       |
| `422` | 請求或者工作區程式碼唔符合優化器嘅要求。                                                                     |

> [!NOTE]
> 一定要傳送 `Idempotency-Key` 標頭。如果你用同一個 key 再傳送同一個請求，Skynet 會回傳第一次嘅運行，唔會開始第二次運行。

## 觀察一次運行

不斷查詢個運行，直到佢嘅狀態去到最終狀態：

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

`status` 欄位係以下其中一個：

| 狀態                               | 最終 |
| ---------------------------------- | ---- |
| `pending`、`validating`、`running` | 唔係 |
| `paused`                           | 唔係 |
| `success`                          | 係   |
| `failed`、`cancelled`、`stopped`   | 係   |

每 10 至 30 秒查詢一次。一次運行可以由幾分鐘去到幾個鐘。

[API 參考](https://api.skynetml.com/scalar) 列出運行回應嘅每個欄位。

## 使用結果

當狀態係 `success`，就攞最佳版本：

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

想用最佳版本處理一個新輸入，就傳送 `POST /serve/{optimization_id}`。每個分數嘅意思，請睇 [睇結果](/docs/guides/results)。

## 限制

API 用同網頁應用程式一樣嘅配額、速率限制同收費。請睇 [限制](/docs/limits) 同 [預算同收費](/docs/concepts/billing)。
