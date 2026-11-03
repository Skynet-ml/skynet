指標話畀 Skynet 知一個輸出有幾好。佢係 `metric.py` 入面嘅一個 Python 函數。每個引擎都會呼叫同一個函數。呢頁講解合約、回傳值同埋例子。

## 合約

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| 參數       | 裝住乜嘢                                         |
| ---------- | ------------------------------------------------ |
| `input`    | 一個 dict，裝住個案例嘅輸入欄位                  |
| `output`   | 你嘅 `run(artifact, input)` 為呢個案例回傳嘅嘢   |
| `expected` | 個案例嘅預期值；如果案例冇預期值，就係 `None`    |

如果你個函數只收兩個參數，Skynet 會呼叫 `score(input, output)`。

對於 DSPy 程式，Skynet 會將 DSPy 嘅指標呼叫轉接到呢個同一個函數。你只需要寫一個指標，就可以用喺每種成品類型。睇 [最佳化點樣運作](/docs/concepts/optimization)。

## 回傳值

`score` 一定要回傳一對值（一個 tuple，或者一個有兩項嘅 list）：

1. **分數。** 一個數字。優化器會盡量令佢更高。
2. **附加資訊。** 一個 dict。佢一定要有 `"feedback"` 鍵，值係一個字串。

附加資訊亦可以有 `"scores"` 鍵。佢將名稱對應到數字，例如 `{"format": 1.0, "accuracy": 0.5}`。

如果回傳值嘅形狀唔啱，合約檢查會喺 **指標回傳值** 呢一步失敗。

| 回傳值                                                 | 有效嗎                             |
| ------------------------------------------------------ | ---------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | 係                                 |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | 係                                 |
| `0.8`                                                  | 唔係。冇附加資訊。                 |
| `(0.8, "Close.")`                                      | 唔係。附加資訊唔係 dict。          |
| `(0.8, {})`                                            | 唔係。冇 feedback 字串。           |
| `("0.8", {"feedback": "OK"})`                          | 唔係。分數係字串。                 |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | 唔係。有個具名分數唔係數字。       |

## 寫有用嘅回饋

GEPA、DSPy GEPA 同 Auto 會讀回饋文字，用嚟寫下一個版本。講清楚錯咗喺邊度，同埋正確嘅輸出應該係點。

| 弱嘅回饋      | 有用嘅回饋                                                           |
| ------------- | -------------------------------------------------------------------- |
| 「錯。」      | 「預期係 'billing'，得到嘅係 'other'。條訊息有提到一筆收費。」       |
| 「格式唔啱。」| 「輸出唔係有效嘅 JSON。最後一個欄位後面多咗個逗號。」                |

> [!NOTE]
> 優化器永遠睇唔到你指標嘅原始碼。用 DSPy 優化器嘅時候，反思模型只會睇到回饋文字。你嘅預期值只會留喺指標入面。

## 例子

### 完全符合

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` 係一個 dict，用你嘅輸出欄位名稱做鍵。睇 [數據集同分割](/docs/concepts/datasets)。

### 幾項檢查

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### 用模型做評判

指標可以呼叫模型。由 `skynet` 輔助模組匯入 `llm`。由指標發出嘅呼叫會去指標模型。

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

指標喺自己嘅 `llm()` 呼叫入面設定模型。顯示嘅四個參數全部都係可選嘅。冇 `model=` 嘅話，指標會用反思模型。

## 準確度、速度同成本

指標可以將準確度同量度到嘅運行時間同模型成本混合埋一齊。喺模組層級定義 `REWARD_SPEC`，再回傳 `reward` 嘅值。

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` 一定要有 `accuracy`、`speed` 同 `cost`。每個權重係 0 或者以上，但唔可以全部都係 0。Skynet 會將佢哋按比例調整，令總和等於 1。
- `speed.reference` 嘅單位係毫秒。`cost.reference` 嘅單位係美元。兩個都一定要係正數。
- 冇 `accuracy.reference` 嘅時候，準確度係 0 至 1 嘅分數。有嘅時候，準確度係一個誤差，越低越好。

Skynet 會自己量度時間同成本。運行程式碼改唔到佢哋。如果 Skynet 量度唔到時間或者成本，嗰部分會得 0 分，回饋亦會講明。

指標亦可以用 `skynet` 嘅 `observation()` 同 `service_cost()` 直接讀取量度結果。

## 限制

| 項目           | 數值                             |
| -------------- | -------------------------------- |
| 指標逾時       | 預設 60 秒，最多 600 秒          |
| 合約檢查樣本   | 3 個案例                         |

## 相關頁面

- [數據集同分割](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交運行](/docs/guides/submit)
