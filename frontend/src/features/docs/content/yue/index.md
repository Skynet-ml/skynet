Skynet 會改良你手上已經有嘅成品。你畀佢一個起始版本、一組案例同一個指標。Skynet 會提出新版本，喺你嘅案例上逐個運行，用你嘅指標幫輸出評分，然後保留最好嗰個版本。

## Skynet 最佳化啲乜

Skynet 處理兩種成品類型：

| 成品類型     | 改動嘅內容                                   |
| ------------ | -------------------------------------------- |
| 倉庫 | 你准改嘅路徑入面嘅檔案 |
| DSPy 程式    | `dspy.Module` 入面各個預測器（predictor）嘅指令 |

每次運行都用同一份合約。你嘅運行程式碼定義 `run(artifact, input)`。你嘅指標定義 `score(input, output, expected=None)`。指標會回傳一個分數同書面回饋。優化器會讀呢啲回饋，令下一個版本更好。

> [!NOTE]
> 你嘅運行程式碼同指標喺沙盒入面、喺唔同嘅程序（process）入面運行。運行程式碼睇唔到指標嘅原始碼，亦都睇唔到預期值。

## 一個產品，好多個引擎

Skynet 就係 Optimize Anything。成品類型決定邊啲引擎可以處理佢：

- 倉庫用原生引擎：GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler 同 ShinkaEvolve。
- DSPy 程式成品用 DSPy 優化器：DSPy GEPA、DSPy MIPROv2 同 DSPy BootstrapFewShot。

喺 DSPy 程式上，**Auto** 會用 DSPy GEPA。喺倉庫上，你揀一個原生引擎。睇吓 [最佳化點樣運作](/docs/concepts/optimization) 同 [引擎](/docs/concepts/engines)。

## 一次運行點樣進行

1. 你描述任務，再設定起始版本。
2. 你提供案例。Skynet 會將佢哋分成訓練、驗證同測試集。
3. 你揀一個優化器、模型同預算。
4. 你寫運行程式碼同指標。合約檢查會喺示例案例上運行佢哋。
5. Skynet 進行最佳化，然後顯示結果。

Skynet 喺驗證案例上揀版本，喺測試案例上報告最終分數。引擎永遠睇唔到測試案例。

## 由邊度開始

| 如果你想                   | 請睇                                                |
| -------------------------- | --------------------------------------------------- |
| 開始你第一次運行           | [快速入門](/docs/quickstart)                        |
| 了解最佳化點樣運作         | [最佳化點樣運作](/docs/concepts/optimization)       |
| 揀一個引擎                 | [引擎](/docs/concepts/engines)                      |
| 準備你嘅數據               | [數據集同分割](/docs/concepts/datasets)             |
| 寫一個指標                 | [指標同評分器](/docs/concepts/scorers)              |
| 填寫精靈                   | [用精靈開始運行](/docs/guides/submit)               |
| 睇你嘅結果                 | [睇結果](/docs/guides/results)                      |
| 了解費用                   | [預算同收費](/docs/concepts/billing)                |
| 由 agent 使用 Skynet       | [MCP](/docs/mcp)                                    |
| 由程式碼呼叫 Skynet        | [API](/docs/api)                                    |
| 查看限制                   | [限制](/docs/limits)                                |

> [!TIP]
> 由一個細嘅數據集同預設設定開始。指標嘅回饋要寫得具體。好嘅回饋畀優化器嘅資訊，遠多過淨係一個分數。
