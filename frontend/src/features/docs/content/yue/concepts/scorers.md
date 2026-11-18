評分器話畀 Skynet 知你倉庫某個版本有幾好。佢係 `scorer.py` 入面嘅一個 Python 函數。每個引擎都會呼叫同一個函數。呢頁講解合約、回傳值同埋例子。

## 合約

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| 參數        | 裝住乜嘢                                                                         |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | 倉庫某個版本嘅資料夾，安裝指令已經喺入面運行過                                   |
| `case`      | 一個 dict。`case["input"]` 裝住輸入欄位。`case["expected"]` 裝住預期值，或者係 `None` |

如果你個函數只收一個參數，Skynet 會呼叫 `score(repo_path)`。如果評分器會運行自己嘅測試、唔需要案例，就用呢個形式。

評分器喺沙盒入面、喺個版本嘅資料夾入面運行。佢可以由倉庫匯入程式碼、運行指令，或者呼叫 `serve.py` 入面嘅 `serve()`。睇 [最佳化點樣運作](/docs/concepts/optimization)。

## 回傳值

`score` 回傳一個物件：

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` 係必須嘅。佢係一個有限嘅數字。只有呢個分數會為版本排名。
- `"feedback"` 係可選嘅文字。建議加上，因為 GEPA 同 Auto 會從佢度學習。冇回饋嘅話，引擎只會見到分數，執行記錄會講一次。
- `"scores"` 係可選嘅。佢將名稱對應到具名分數。每個具名分數係一個物件，有自己嘅 `"score"` 同一個可選嘅 `"feedback"`。每個引擎都會將每個具名分數當做一個獨立目標。

其他任何形狀或者鍵都係錯誤。檢查會顯示錯咗嘅部分同有效嘅形狀。

| 回傳值 | 有冇效 |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | 係 |
| `{"score": 0.8}` | 係。冇回饋。 |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | 係 |
| `0.8` | 唔係。個值唔係物件。 |
| `(0.8, "Close. Missing the date.")` | 唔係。個值唔係物件。 |
| `{"score": "0.8"}` | 唔係。分數係字串。 |
| `{"score": 0.8, "scores": {"format": 1.0}}` | 唔係。具名分數一定要係物件。 |
| `{"score": 0.8, "feedbak": "OK"}` | 唔係。`feedbak` 呢個鍵唔存在。 |

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
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` 係一個 dict，用你嘅輸出欄位名稱做鍵。睇 [數據集同分割](/docs/concepts/datasets)。

### 運行倉庫嘅測試

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
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
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
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
