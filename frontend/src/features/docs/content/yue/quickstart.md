呢頁教你點樣喺大約十分鐘內開始你第一次運行。你會登入、上載一個細嘅數據集、填好精靈嘅四個步驟，然後開始運行。例子會改良一個將客服訊息分類做唔同標籤嘅 DSPy 程式。

## 開始之前

你需要：

- 一個 Skynet 帳戶。
- 一個 CSV、JSON 或者 Excel 檔案（`.csv`、`.json`、`.xlsx` 或 `.xls`）。每一列係一個案例。
- 一個或者多個輸入欄，例如 `message`。
- 一個預期輸出欄，例如 `label`。

> [!TIP]
> 最少用 5 列。如果少過 5 對，Skynet 唔會保留測試集，咁最終分數就會嚟自優化器用過嘅案例。

## 1. 登入

登入 Skynet。然後打開提交頁面 `/submit`。

> [!NOTE]
> 呢頁用嘅係 **標準** 程度嘅標籤。**引導** 程度用較簡單嘅標籤，會略過優化器步驟，並幫你揀 **Auto**。你可以喺設定度改程度。

## 2. 任務

第一步係 **任務**。佢會問「你想優化啲咩？」

1. 喺 **目標**，寫低咩先算好結果。用淺白嘅字，例如「將每條客服訊息分類做 billing、bug 或者 other。」
2. 可選：喺 **背景**，加啲可以幫到優化器嘅背景資料。
3. 喺 **優化對象**，揀 **DSPy 程式**。
4. 喺 **起始版本**，貼上你程式而家嘅指示。

## 3. 輸入

第二步係 **輸入**。

1. 揀 **上載 CSV、JSON 或 Excel 檔案**，或者揀 **由資料庫揀** 去用一個已儲存嘅數據集。
2. 喺 **點樣讀取資料** 下面，幫每一欄設定角色：
   - **輸入**：運行程式碼會收到呢一欄。
   - **輸出**：預期值。指標會收到佢。
   - **略過**：Skynet 唔會用呢一欄。
3. 分割保留 **使用建議設定**。大部分數據集嘅分割係 70% 訓練、15% 驗證同 15% 測試。

完整規則請睇 [數據集同分割](/docs/concepts/datasets)。

## 4. 優化器

第三步係 **優化器**（「優化器、模型同預算」）。

- 優化器保留 **Auto**。
- 揀一個 **反思模型**。優化器用佢嚟寫新版本。呢個模型係必填。
- 揀一個 **任務模型**。你嘅程式會用佢。DSPy 程式一定要有。
- 喺 **預算** 下面，**評分器最多運行次數** 保留預設嘅 200。

想揀指定引擎，請睇 [引擎](/docs/concepts/engines)。

## 5. 指標同執行

最後一步係 **指標同執行**（「程式碼同指標」）。你要寫兩個檔案。

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` 係最佳化器提出嘅而家嘅指示。Skynet 會將佢套用落 `program`。`input` 裝住一個案例嘅輸入欄。`expected` 裝住輸出欄，用欄名做 key。

然後喺 **合約檢查** 下面揀 **檢查**。檢查會喺沙盒入面，用 3 個示例案例運行你嘅程式碼同指標。佢會檢查：

| 檢查               | 咩情況會失敗                                         |
| ------------------ | ---------------------------------------------------- |
| 執行程式碼簽名     | `run.py` 冇定義 `run(artifact, input)`               |
| 指標簽名           | `metric.py` 冇定義 `score(input, output, expected=None)` |
| 指標模型           | 指標模型用唔到                                       |
| 喺示例上執行程式碼 | 你嘅運行程式碼喺某個示例案例上出錯                   |
| 指標回傳值         | `score` 冇回傳有效嘅分數同回饋                       |

> [!NOTE]
> 合約檢查通過之後，你先可以開始運行。

## 6. 開始同觀察

摘要畫面會顯示「準備好執行」。佢嘅分頁有一般、數據、模型、預算同獎勵。檢查好設定，然後開始最佳化。

喺你嘅最佳化列表打開個運行，就可以觀察佢。想了解分數同勝出版本，請睇 [睇結果](/docs/guides/results)。

## 下一步

- 寫一個更好嘅指標：[指標同評分器](/docs/concepts/scorers)。
- 了解邊啲引擎適合你嘅成品：[最佳化點樣運作](/docs/concepts/optimization)。
- 了解一次運行要幾多錢：[預算同收費](/docs/concepts/billing)。
