Skynet 會改良你手上已經有嘅程式碼，或者佢幫你寫嘅程式碼。Skynet 就係 Optimize Anything：每次運行都係最佳化一個倉庫。你畀佢一個倉庫、一組案例同一個評分器。Skynet 會提出倉庫嘅新版本，用你嘅評分器幫每個版本評分，然後保留最好嗰個版本。

## Skynet 最佳化啲乜

每次運行都係最佳化一個倉庫。你可以用兩種方式開始：

| 開始方式   | 會發生咩事                                                        |
| ---------- | ----------------------------------------------------------------- |
| 用現有倉庫   | Skynet 會改你連接嘅 GitHub 倉庫入面、你准改嘅路徑入面嘅檔案。       |
| 建立新倉庫 | 你畀任務同數據。Skynet 會喺你嘅帳戶建立一個新嘅 GitHub 倉庫，再寫第一個解決方案。 |

倉庫入面嘅程式碼可以係任何嘢：應用程式、服務、腳本或者函式庫。佢可以用任何函式庫。

每次運行都用同一份合約。你嘅評分器定義 `score(repo_path, case)`。佢會回傳一個分數同書面回饋。優化器會讀呢啲回饋，令下一個版本更好。

> [!NOTE]
> 每個版本都喺沙盒入面運行。寫版本嘅編程 agent 睇唔到評分器嘅原始碼，亦都睇唔到預期值。

## 一個產品，好多個引擎

**Auto** 係每次運行嘅預設選項。佢會試 GEPA、AutoResearch、Meta-Harness、AutoSaddler 同 ShinkaEvolve，然後由最好嗰個版本繼續行 GEPA。你亦都可以揀一個引擎：GEPA、Best-of-N、AutoResearch、Meta-Harness、AutoSaddler 或者 ShinkaEvolve。睇吓 [最佳化點樣運作](/docs/concepts/optimization) 同 [引擎](/docs/concepts/engines)。

## 一次運行點樣進行

1. 你描述任務，然後揀一個倉庫，或者叫 Skynet 建立一個。
2. 你提供案例。Skynet 會將佢哋分成訓練、驗證同測試集。
3. 你揀一個優化器、模型同預算。
4. 你寫評分器。設定檢查會喺起始版本上運行佢。
5. Skynet 進行最佳化，然後顯示結果。

Skynet 喺驗證案例上揀版本，喺測試案例上報告最終分數。引擎永遠睇唔到測試案例。

## 由邊度開始

| 如果你想                   | 請睇                                                |
| -------------------------- | --------------------------------------------------- |
| 開始你第一次運行           | [快速入門](/docs/quickstart)                        |
| 了解最佳化點樣運作         | [最佳化點樣運作](/docs/concepts/optimization)       |
| 揀一個引擎                 | [引擎](/docs/concepts/engines)                      |
| 準備你嘅數據               | [數據集同分割](/docs/concepts/datasets)             |
| 寫一個評分器               | [指標同評分器](/docs/concepts/scorers)              |
| 填寫精靈                   | [用精靈開始運行](/docs/guides/submit)               |
| 睇你嘅結果                 | [睇結果](/docs/guides/results)                      |
| 了解費用                   | [預算同收費](/docs/concepts/billing)                |
| 由 agent 使用 Skynet       | [MCP](/docs/mcp)                                    |
| 由程式碼呼叫 Skynet        | [API](/docs/api)                                    |
| 查看限制                   | [限制](/docs/limits)                                |

> [!TIP]
> 由一個細嘅數據集同預設設定開始。評分器嘅回饋要寫得具體。好嘅回饋畀優化器嘅資訊，遠多過淨係一個分數。
