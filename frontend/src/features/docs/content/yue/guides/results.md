運行頁面會顯示一次運行嘅進度同結果。呢份指南講解運行頁面嘅分頁、分數，同埋點樣匯出或者提供最佳版本。

## 運行頁面嘅分頁

運行頁面只會顯示適用於呢次運行嘅分頁。有啲分頁取決於狀態同你嘅經驗程度。

| 分頁               | 幾時顯示                                       | 內容                                               |
| ------------------ | ---------------------------------------------- | -------------------------------------------------- |
| **概覽**           | 一直顯示                                       | 進度、分數同分數歷史                               |
| **用量**           | 成功嘅運行，而且佢個倉庫有 `serve.py` 檔案     | 呼叫最佳版本嘅表格，同埋整合程式碼                 |
| **成品**           | 有結果嘅運行                                   | 你個倉庫嘅各個版本，連埋最佳版本                   |
| **資料**           | 已完成而且有案例嘅運行                         | 分成訓練集、驗證集同測試集嘅資料                   |
| **程式碼**         | 運行有程式碼可以顯示嘅時候                     | 評分器                                             |
| **獎勵**           | 評分器有宣告獎勵部分嘅運行                     | 獎勵部分同佢哋嘅權重                               |
| **記錄**           | 大部分運行                                     | 運行記錄                                           |
| **用量同費用**     | 你登入咗嘅時候                                 | 運行用咗幾多錢                                     |
| **排程執行**       | 成功嘅運行                                     | 由最佳版本再次運行最佳化嘅排程                     |
| **設定**           | 一直顯示                                       | 運行設定                                           |

> [!NOTE]
> 喺 **引導** 程度，運行頁面會隱藏 **資料**、**程式碼**、**記錄** 同 **設定** 分頁。直接連去其中一個分頁嘅連結仍然打得開。喺 **設定 → 帳戶** 改程度。

## 分數

Skynet 用驗證案例揀版本。佢用測試案例報告最終分數。引擎永遠睇唔到測試案例。睇 [數據集同分割](/docs/concepts/datasets)。

**概覽** 分頁會顯示：

- **提升幅度**：由起始版本到最佳版本嘅變化。
- **分數進度**：運行期間嘅分數。
- **評分細項**：每個分數，連埋 **基準**、**已最佳化** 同 **變動** 欄。

運行進行緊嘅時候，概覽亦會顯示 **已耗時**、**剩餘** 同 **速率**。

當你嘅評分器報告具名分數時，概覽亦會顯示 **命名分數**。

### 當最佳版本就係起始版本

有時冇任何候選版本贏到起始版本。咁 Skynet 就會返回起始版本。**成品** 分頁會話你知用咗回歸保護。

如果運行喺預算停止，運行頁面會話你知 Skynet 儲存咗邊個版本。睇 [運行](/docs/concepts/runs)。

## 匯出最佳版本

**成品** 分頁會列出你個倉庫嘅 **版本**。每個版本你都可以：

- 複製呢個版本。
- 將呢個版本下載成檔案。
- 將呢個版本下載成成個倉庫嘅 zip。

你可以將一個版本同另一個版本比較。分頁亦會顯示一個版本嘅 **評分器嘅回饋**。

### 新倉庫入面嘅分支

如果 Skynet 為呢次運行建立咗個倉庫，佢會將每個候選版本推去佢自己嘅分支 `skynet/candidate-<version id>`。`skynet/best` 分支一直指住最佳版本。有更好嘅版本出現時，佢會跟住移動。

## 提供最佳版本

你可以喺你自己嘅程式碼呼叫最佳版本。個倉庫嘅根目錄一定要有一個 `serve.py` 檔案，入面定義 `serve(input)`：

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` 係請求入面輸入欄位嘅 dict。
- 回傳值一定要可以序列化成 JSON。dict 會變成 `outputs`。其他任何值會變成 `{"output": value}`。
- `serve.py` 可以用 `skynet` 輔助模組嘅 `llm` 呼叫模型。佢唔會攞到運行嘅密鑰。

Skynet 喺同運行一樣嘅受保護沙盒入面運行最佳版本。**用量** 分頁有兩部分：

- 一個表格。輸入啲輸入欄位，再揀 **執行**，就會得到一個結果。
- cURL、Python、JavaScript 同 Go 嘅 **整合程式碼**。

想呼叫最佳版本，喺 **設定 → API** 建立一個 API token。然後發送請求。一定要有 `Idempotency-Key` 標頭：

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

將 `message` 換成你今次運行嘅輸入欄位。**用量** 分頁上面嘅 **整合程式碼** 會幫你填好真正嘅網址同欄位名稱。

回應包括 `outputs`、`input_fields`、`output_fields`、`model_used`、`cents_charged` 同 `budget`。

> [!TIP]
> 每次呼叫都會分開收費，由呼叫者嘅餘額扣。`max_cost_cents` 設定呢一次呼叫最多可以用幾多錢，以仙計。如果你再發送同一個 `Idempotency-Key`，Skynet 唔會再收一次費。

身份驗證同其他端點，睇 [API](/docs/api)。

## 分享結果

你可以同其他人分享一次運行，或者將佢列喺公開嘅探索圖庫。睇 [分享同發佈](/docs/guides/sharing)。
