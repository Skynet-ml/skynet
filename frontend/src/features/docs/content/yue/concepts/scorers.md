評分器話畀 Skynet 知你倉庫某個版本有幾好。佢係 `scorer.py` 入面嘅一個 Python 函數。每個引擎都會呼叫同一個函數。呢頁講解合約、回傳值同埋例子。

## 合約

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| 參數        | 裝住乜嘢                                                                         |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | 一個只裝住你啲指標檔案嘅資料夾。佢冇其他倉庫檔案，亦冇版本程式碼 |
| `case`      | 一個 dict。`case["input"]` 裝住輸入欄位。`case["expected"]` 裝住預期值，或者係 `None` |
| `output`    | `[run]` 入口為呢個案例回傳嘅嘢；如果檔案冇 `[run]`，就係 `None` |

倉庫嘅 `ascent.toml` 檔案一定要有一個 `[run]` 部分或者一個 `[target.<name>]` 階段。`[run]` 嘅 `entry` 會喺自己嘅沙盒入面，用一個案例運行一個版本。如果兩樣都冇，Skynet 會拒絕個檔案，因為每個版本都會攞到一樣嘅分數。

- 函數入口，例如 `entry = "serve.py:serve"`，會收到 `case["input"]` 做參數。`output` 係佢嘅回傳值。
- 指令入口，例如 `entry = ["python", "run_tests.py"]`，會由 `ASCENT_INPUT` 路徑嘅 JSON 檔案讀 `{"input": ...}`。佢會將 `{"output": ...}` 寫去 `ASCENT_OUTPUT` 路徑。`output` 係 `"output"` 下面嘅值。指令印出嚟嘅嘢只會去日誌。

評分器喺另一個獨立嘅沙盒入面運行。嗰個沙盒只裝住你啲指標檔案。評分器唔可以匯入或者運行版本嘅程式碼。佢只可以透過 `output`，或者透過對 `[target.<name>]` 階段嘅 `call()` 睇到個版本。用 `from openascent import call` 匯入 `call`。睇 [最佳化點樣運作](/docs/concepts/optimization)。

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

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` 係一個 dict，用你嘅輸出欄位名稱做鍵。睇 [數據集同分割](/docs/concepts/datasets)。

### 運行倉庫嘅測試

`[run]` 入口會運行測試。有測試失敗嘅時候，`pytest` 會用非零結束碼結束，而非零結束碼會令個案例失敗。所以包裝腳本 `run_tests.py` 會將結果寫去 `ASCENT_OUTPUT`，再用 0 結束。評分器會讀呢個結果。

```toml tab="ascent.toml"
version = 1

[run]
entry = ["python", "run_tests.py"]
timeout = 300
```

```python tab="run_tests.py"
import json
import os
import subprocess

result = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True)
with open(os.environ["ASCENT_OUTPUT"], "w") as f:
    json.dump({"output": {"passed": result.returncode == 0, "log": result.stdout[-2000:]}}, f)
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["passed"]:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["log"]}
```

如果 `[run]` 入口喺某個案例失敗，嗰個案例就冇分數。如果入口開唔到、超時，或者俾信號停咗，又或者函數入口喺返回之前拋出異常或者退出，回饋就係 `Run failed: …`，Skynet 唔會呼叫評分器。命令入口用非零結束碼結束，或者冇寫出有效嘅輸出檔案，都會失敗。有呢種案例嘅版本冇分數。佢唔可以係最好嘅版本，唔會達到任何 `[outcome] target`，亦唔會開 pull request。如果失敗一定要算做一個數字，就喺你個程式入面捕捉個失敗，寫一個輸出，再俾評分器幫佢評分。睇 [儲存庫規則](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md)。

### 用模型做評判

評分器可以呼叫模型。由 `openascent` 輔助模組匯入 `llm`。呼叫會經 Skynet 嘅模型閘道，去到今次運行嘅評分器模型。

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from openascent import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

評分器亦可以用任何兼容 OpenAI 嘅客戶端，配合 `OPENAI_BASE_URL`、`ASCENT_MODEL_KEY` 同 `ASCENT_MODEL` 環境變數。閘道只接受今次運行嘅評分器模型。

## 模型呼叫同密鑰

- 用 Skynet 金鑰嘅呼叫會計入運行預算。用你自己嘅金鑰（BYOK）嘅話，呼叫會計落你嘅供應商金鑰，Skynet 唔收模型費用。睇 [預算同收費](/docs/concepts/billing)。
- 評分器唔會以環境變數嘅形式收到運行密鑰。密鑰只可以經 `[metric] services` 列出嘅 `[services.<name>]` 表，或者 `[metric] mcp` 檔案入面嘅伺服器，去到評分器。將密鑰以 `${NAME}` 嘅形式寫喺個表嘅 `headers` 入面。評分器會將請求發去 `ASCENT_SERVICE_<NAME>_URL` 入面嘅 URL。Skynet 會喺沙盒外面加入密鑰。
- Skynet 會喺評分器輸出同日誌入面隱藏密鑰嘅值。

## 限制

| 項目       | 數值                    |
| ---------- | ----------------------- |
| 評分器逾時 | 預設 60 秒，最多 600 秒 |

## 相關頁面

- [數據集同分割](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交運行](/docs/guides/submit)
