評分器話畀 Skynet 知你倉庫某個版本有幾好。佢係 `scorer.py` 入面嘅一個 Python 函數。每個引擎都會呼叫同一個函數。呢頁講解合約、回傳值同埋例子。

## 合約

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| 參數        | 裝住乜嘢                                                                         |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | 倉庫某個版本嘅資料夾，安裝指令已經喺入面運行過                                   |
| `case`      | 一個 dict。`case["input"]` 裝住輸入欄位。`case["expected"]` 裝住預期值，或者係 `None` |

如果你個函數只收一個參數，Skynet 會呼叫 `score(repo_path)`。如果評分器會運行自己嘅測試、唔需要案例，就用呢個形式。

評分器喺沙盒入面、喺個版本嘅資料夾入面運行。佢可以由倉庫匯入程式碼、運行指令，或者呼叫 `serve.py` 入面嘅 `serve()`。睇 [最佳化點樣運作](/docs/concepts/optimization)。

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

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` 係一個 dict，用你嘅輸出欄位名稱做鍵。睇 [數據集同分割](/docs/concepts/datasets)。

### 運行倉庫嘅測試

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### 用模型做評判

評分器可以呼叫模型。由 `skynet` 輔助模組匯入 `llm`。呼叫會經 Skynet 嘅模型閘道，去到今次運行嘅評分器模型。

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

評分器亦可以用任何兼容 OpenAI 嘅客戶端，配合 `OPENAI_BASE_URL`、`APP_API_KEY` 同 `APP_MODEL` 環境變數。閘道只接受今次運行嘅評分器模型。

## 模型呼叫同密鑰

- 用 Skynet 金鑰嘅呼叫會計入運行預算。用你自己嘅金鑰（BYOK）嘅話，呼叫會計落你嘅供應商金鑰，Skynet 唔收模型費用。睇 [預算同收費](/docs/concepts/billing)。
- 運行密鑰會以環境變數嘅形式去到評分器。變數名就係密鑰名，例如 `os.environ["SEARCH_API_KEY"]`。
- Skynet 會喺評分器輸出同日誌入面隱藏密鑰嘅值。

## 限制

| 項目       | 數值                    |
| ---------- | ----------------------- |
| 評分器逾時 | 預設 60 秒，最多 600 秒 |

## 相關頁面

- [數據集同分割](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交運行](/docs/guides/submit)
