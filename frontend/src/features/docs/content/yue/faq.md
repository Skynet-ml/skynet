呢頁解答關於 Skynet 嘅常見問題。每個答案都有連結去講齊細節嘅頁面。

## 一般

### Skynet 係乜嘢？

Skynet 係一個按你自己嘅數據同評分器去最佳化倉庫嘅平台。你上載一個數據集、描述任務，然後開始運行。運行會試新版本，再喺預留起嘅數據上幫佢哋評分。想開始，請睇 [快速入門](/docs/quickstart)。

### Skynet 可以最佳化啲乜？

Skynet 就係 Optimize Anything：每次運行都係最佳化一個倉庫。你可以用你已經有嘅 GitHub 倉庫，或者叫 Skynet 根據你嘅任務同數據建立一個新倉庫。程式碼可以用任何函式庫。

每個引擎都處理倉庫：GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler 同 ShinkaEvolve。

**Auto** 係預設選項。佢會先用幾個引擎探索，然後由最好嗰個版本繼續行 GEPA。睇吓 [最佳化點樣運作](/docs/concepts/optimization) 同 [引擎](/docs/concepts/engines)。

### 介面支援邊啲語言？

介面有 24 種語言。希伯來文係基本語言，版面支援由右至左嘅文字。希伯來文文件喺 [/docs/he](/docs/he)。

### Skynet 係咪開源？

係。程式碼用 AGPL-3.0 授權。如果你將改過嘅 Skynet 版本當服務提供，授權要求你公開你嘅改動。

## 數據

### 我可以上載邊啲檔案類型？

你可以上載 CSV、JSON、`.xlsx` 同 `.xls` 檔案。Excel 檔案方面，Skynet 只會讀第一張工作表。請睇 [準備數據集](/docs/guides/datasets)。

### 我嘅數據冇標籤，可以點？

用標註工具。你可以自己標註資料列，或者由 AI 模型標註，你再審核佢做嘅嘢。請睇 [用標註工具標註數據](/docs/guides/tagger)。

### 邊個可以睇到我嘅運行同數據集？

> [!WARNING]
> 新運行預設係 **公開**。公開運行會出現喺公開搜尋入面，任何人都可以瀏覽佢嘅結果。

提交運行嗰陣將 **瀏覽權限** 設做 **私人**，就可以唔畀佢出現喺公開搜尋。新數據集係 **已限制**：只有你同你邀請嘅人可以打開。請睇 [分享同發佈](/docs/guides/sharing) 同 [安全](/docs/security)。

### 模型供應商會唔會用我嘅提示詞嚟訓練？

預設係唔會。**模型數據私隱** 嘅預設選項係 **唔准訓練**。佢只會將提示詞傳送畀唔會用佢哋嚟訓練、亦唔會保留佢哋嘅供應商。呢個設定適用於 Skynet 由你結餘扣費嘅呼叫。經 OpenRouter 嘅 BYOK 呼叫會跟你 OpenRouter 帳戶嘅私隱設定。直接連接供應商嘅 BYOK 呼叫會跟該供應商嘅數據政策。請睇 [安全](/docs/security)。

## 收費

### Skynet 點樣為運行收費？

Skynet 用預付結餘。託管模型用量同加價，以及沙盒用量都會計量。增值時會收手續費。付費工作開始之前，Skynet 會喺你嘅錢包同運行預算入面預留空間。請睇 [預算同收費](/docs/concepts/billing)。

### 我可唔可以用自己嘅模型供應商金鑰？

可以。用自己嘅金鑰（BYOK）時，供應商會將模型 token 費用記喺你嘅金鑰。Skynet 唔收模型費。運行預算只包括沙盒用量。Skynet 會加密你嘅金鑰，之後唔會再顯示佢。請睇 [預算同收費](/docs/concepts/billing) 同 [安全](/docs/security)。

### 我可唔可以唔使為模型呼叫畀錢就運行 Skynet？

[Mac 應用程式](/docs/guides/mac-app) 喺你自己部電腦運行 Skynet。佢經喺你部 Mac 登入咗嘅 Claude Code 或者 Codex 傳送模型呼叫。呢啲呼叫由你嘅 CLI 方案支付，所以呢啲呼叫喺運行入面嘅費用係 $0。

## 工具同整合

### 我可唔可以用自己嘅 AI 用戶端控制 Skynet？

可以。Skynet 有一個 MCP 伺服器，喺 `https://mcp.skynetml.com/mcp`。你用 OAuth 登入。伺服器有呢啲工具：`check_run`、`start_run`、`get_run`、`list_runs` 同 `stop_run`。請睇 [MCP](/docs/mcp)。

### 有冇 API？

有。喺設定度產生一個 API Token，然後用 `Authorization: Bearer <token>` 傳送。每個用戶同一時間只有一個有效 Token。請睇 [API](/docs/api)。

### Agent 面板可唔可以刪除我嘅運行？

只有你允許先可以。喺預設嘅 **每次詢問** 模式，agent 做每個動作之前都會問你。喺 **自動** 模式，agent 做破壞性動作之前（例如刪除或者提交）仍然會問你。請睇 [Agent 面板](/docs/guides/agent-panel)。
