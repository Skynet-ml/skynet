Skynet 就係 Optimize Anything：一個按你嘅指標改良 DSPy 程式或者倉庫嘅產品。成品類型決定邊啲引擎可以處理佢。

## 共用合約

每次運行都有兩個檔案。`run.py` 喺一個案例上運行成品。`metric.py` 幫輸出評分。

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact` 係優化器提出嘅當前版本。
- `input` 係一個 dict，裝住一個案例嘅輸入欄位。
- `expected` 係案例嘅預期值；如果案例冇預期值，就係 `None`。
- `score` 回傳一對值：一個數字，同一個包含 `"feedback"` 字串嘅 dict。

`skynet` 輔助模組提供 `llm`、`Image` 同 `log`。由運行程式碼呼叫 `llm`，會去任務模型。由指標呼叫 `llm`，會去指標模型。

> [!NOTE]
> 運行程式碼喺沙盒入面自己嘅程序（process）運行。佢睇唔到指標原始碼、預期值，亦都睇唔到指標金鑰。

完整嘅指標合約，請睇 [指標同評分器](/docs/concepts/scorers)。

## 成品類型同引擎

| 成品類型   | 引擎                                                                         |
| ---------- | ---------------------------------------------------------------------------- |
| 倉庫 | GEPA、Best-of-N、AutoResearch、Meta-Harness、AutoSaddler、ShinkaEvolve |
| DSPy 程式  | Auto、DSPy GEPA、DSPy MIPROv2、DSPy BootstrapFewShot                         |

引擎分兩大類：

- **原生引擎** 將成品當做黑盒處理。佢哋用喺倉庫上。
- **DSPy 優化器** 喺 DSPy 程式入面運作。佢哋只用喺 DSPy 程式成品上。

DSPy MIPROv2 同 DSPy BootstrapFewShot 需要成對數據（有預期輸出嘅案例）。如果得單一輸入，DSPy 程式只可以用 Auto 同 DSPy GEPA。

## 倉庫

原生引擎會改你准改嘅路徑入面嘅檔案。你嘅指標會為倉庫嘅每個版本評分。

六個原生引擎用唔同嘅搜尋方法：

- **GEPA** 透過反思去演化版本，並保留一個 Pareto 前沿。
- **Best-of-N** 叫反思模型提出獨立嘅提案，然後保留最好嗰個。
- **AutoResearch**、**Meta-Harness**、**AutoSaddler** 同 **ShinkaEvolve** 用編程代理去提出版本。

每個引擎同佢嘅設定，請睇 [引擎](/docs/concepts/engines)。

## DSPy 程式成品

DSPy 程式係一種成品類型。你嘅 `run.py` 喺模組層級建立一個 `dspy.Module`，再喺 `run` 入面呼叫佢。如果檔案有多過一個模組，就將要最佳化嗰個命名為 `program`。

DSPy 優化器運行時會發生乜事：

1. Skynet 搵到你嘅模組，再用你嘅 `run` 函數包住佢。每個案例都會經過你自己嘅 `run`。
2. DSPy 優化器喺訓練案例上編譯個程式。
3. 佢喺驗證案例上揀選。
4. 勝出者係每個預測器嘅指令文字，加上模組完整嘅編譯狀態。呢個狀態包括 few-shot 示範。

最終分數量度嘅，就係 Skynet 儲存嗰個程式。

| 優化器                | 讀唔讀回饋文字   |
| --------------------- | ---------------- |
| DSPy GEPA             | 讀               |
| DSPy MIPROv2          | 唔讀，只睇分數   |
| DSPy BootstrapFewShot | 唔讀，只睇分數   |

DSPy BootstrapFewShot 唔用反思模型。

> [!TIP]
> DSPy 反思模型只睇到你嘅指標回傳嘅回饋文字，永遠睇唔到指標原始碼。

## Auto

**Auto** 係精靈入面嘅預設選擇。

- **倉庫**：Auto 用唔到。揀一個原生引擎。
- 喺 DSPy 程式上，Auto 運行 DSPy GEPA。

Auto 會讀你指標嘅回饋文字。

## 揀邊個引擎

| 你嘅情況                                       | 用乜                                                          |
| ---------------------------------------------- | ------------------------------------------------------------- |
| 你喺 GitHub 倉庫有程式碼 | 倉庫。揀一個原生引擎。 |
| 你有一個 DSPy 程式                             | DSPy 程式成品。由 Auto 開始。                                 |
| 你想由你嘅數據揀出 few-shot 示範               | DSPy 程式成品，配 DSPy BootstrapFewShot 或者 DSPy MIPROv2     |

> [!WARNING]
> GEPA、DSPy GEPA 同 Auto 會由回饋文字學習。如果你嘅回饋永遠係同一句字串，呢啲優化器可以用嘅資訊就會少咗。

## 相關頁面

- [引擎](/docs/concepts/engines)
- [數據集同分割](/docs/concepts/datasets)
- [指標同評分器](/docs/concepts/scorers)
- [用精靈開始運行](/docs/guides/submit)
