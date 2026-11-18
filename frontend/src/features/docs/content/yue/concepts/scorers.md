評分器話畀 Skynet 知你倉庫某個版本有幾好。佢係 `scorer.py` 入面嘅一個 Python 函數。每個引擎都會呼叫同一個函數。呢頁講解合約、回傳值同埋例子。

## 合約

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| 參數        | 裝住乜嘢                                                                         |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | 一個只裝住你啲指標檔案嘅資料夾。佢冇其他倉庫檔案，亦冇版本程式碼 |
| `case`      | 一個 dict。`case["input"]` 裝住輸入欄位。`case["expected"]` 裝住預期值，或者係 `None` |
| `output`    | `[run]` 入口為呢個案例回傳嘅嘢；如果檔案冇 `[run]`，就係 `None` |

倉庫嘅 `optimize.toml` 檔案一定要有一個 `[run]` 部分或者一個 `[target.<name>]` 階段。`[run]` 嘅 `entry` 會喺自己嘅沙盒入面，用一個案例運行一個版本。如果兩樣都冇，Skynet 會拒絕個檔案，因為每個版本都會攞到一樣嘅分數。

- 函數入口，例如 `entry = "serve.py:serve"`，會收到 `case["input"]` 做參數。`output` 係佢嘅回傳值。
- 指令入口，例如 `entry = ["python", "-m", "pytest", "-q"]`，會由 stdin 收到 JSON 格式嘅輸入。`output` 係 `{"exit_code": ..., "stdout": "..."}`。

評分器喺另一個獨立嘅沙盒入面運行。嗰個沙盒只裝住你啲指標檔案。評分器唔可以匯入或者運行版本嘅程式碼。佢只可以透過 `output`，或者透過對 `[target.<name>]` 階段嘅 `call()` 睇到個版本。用 `from skynet import call` 匯入 `call`。睇 [最佳化點樣運作](/docs/concepts/optimization)。

## 回傳值

`score` 一定要回傳一個分數同回饋。以下形狀係有效嘅：

1. 一對值：一個數字，同一個回饋字串或者一個有 `"feedback"` 鍵嘅 dict。
2. 一個 dict，`"score"` 下面係一個數字，`"feedback"` 下面係一個字串。

回饋一定要係一個唔係空嘅字串。

附加資訊亦可以有 `"scores"` 鍵。佢將名稱對應到具名分數。每個具名分數都有自己嘅分數同回饋，例如 `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`。

附加資訊亦可以有一個形狀一樣嘅 `"cases"` 鍵。每一項係一個案例嘅結果。每個引擎都會將每個案例當做一個獨立目標。

如果回傳值嘅形狀唔啱，檢查會失敗，並顯示原因。

| 回傳值 | 有冇效 |
| --- | --- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | 係 |
| `(0.8, "Close. Missing the date.")`                                             | 係 |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | 係 |
| `0.8`                                                                           | 唔係。冇回饋。 |
| `(0.8, {})`                                                                     | 唔係。冇回饋字串。 |
| `("0.8", {"feedback": "OK"})`                                                   | 唔係。分數係字串。 |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | 唔係。有個具名分數唔係數字。 |

## 寫有用嘅回饋

GEPA 同 Auto 會讀回饋文字，用嚟寫下一個版本。講清楚錯咗喺邊度，同埋正確嘅輸出應該係點。

| 弱嘅回饋       | 有用嘅回饋                                                     |
| -------------- | -------------------------------------------------------------- |
| 「錯。」       | 「預期係 'billing'，得到嘅係 'other'。條訊息有提到一筆收費。」 |
| 「格式唔啱。」 | 「輸出唔係有效嘅 JSON。最後一個欄位後面多咗個逗號。」          |

> [!NOTE]
> 編程 agent 永遠睇唔到你評分器嘅原始碼、預期值或者運行密鑰。佢淨係睇到分數同回饋文字。

## 例子

### 完全符合

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` 係一個 dict，用你嘅輸出欄位名稱做鍵。睇 [數據集同分割](/docs/concepts/datasets)。

### 運行倉庫嘅測試

`[run]` 入口會運行測試。評分器會讀結束碼同測試輸出。

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

如果一個版本嘅 `[run]` 入口失敗，Skynet 會自己俾嗰個版本 0 分。如果入口開唔到、超時，或者俾信號停咗，回饋就係 `Run failed: …`，Skynet 唔會呼叫評分器。非零嘅結束碼唔算失敗：評分器會喺 `output` 收到佢。

### 用模型做評判

評分器可以呼叫模型。由 `skynet` 輔助模組匯入 `llm`。呼叫會經 Skynet 嘅模型閘道，去到今次運行嘅評分器模型。

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

評分器亦可以用任何兼容 OpenAI 嘅客戶端，配合 `OPENAI_BASE_URL`、`APP_API_KEY` 同 `APP_MODEL` 環境變數。閘道只接受今次運行嘅評分器模型。

## 模型呼叫同密鑰

- 用 Skynet 金鑰嘅呼叫會計入運行預算。用你自己嘅金鑰（BYOK）嘅話，呼叫會計落你嘅供應商金鑰，Skynet 唔收模型費用。睇 [預算同收費](/docs/concepts/billing)。
- 評分器唔會以環境變數嘅形式收到運行密鑰。密鑰只可以經 `[metric] services` 列出嘅 `[services.<name>]` 表去到評分器。將密鑰以 `${NAME}` 嘅形式寫喺個表嘅 `headers` 入面。評分器會將請求發去 `SKYNET_SERVICE_<NAME>_URL` 入面嘅 URL。Skynet 會喺沙盒外面加入密鑰。
- Skynet 會喺評分器輸出同日誌入面隱藏密鑰嘅值。

## 限制

| 項目       | 數值                    |
| ---------- | ----------------------- |
| 評分器逾時 | 預設 60 秒，最多 600 秒 |

## 相關頁面

- [數據集同分割](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交運行](/docs/guides/submit)
