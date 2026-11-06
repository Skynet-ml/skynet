本页回答关于 Skynet 的常见问题。每个回答都链接到包含完整细节的页面。

## 常规

### 什么是 Skynet？

Skynet 是一个根据你自己的数据和指标优化 DSPy 程序和仓库的平台。你上传数据集、描述任务，然后开始运行。运行会尝试新版本，并在留出的数据上为它们打分。要开始使用，请阅读[快速入门](/docs/quickstart)。

### Skynet 能优化什么？

Skynet 就是 Optimize Anything。它在一个产品中优化 DSPy 程序和仓库。产物类型决定哪些引擎可以处理它：

- 仓库使用原生引擎：GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler 和 ShinkaEvolve。
- DSPy 程序使用 DSPy 优化器：DSPy GEPA、DSPy MIPROv2 和 DSPy BootstrapFewShot。

对于 DSPy 程序，**Auto** 使用 DSPy GEPA。对于仓库，你需要选择一个原生引擎。请阅读 [优化的工作原理](/docs/concepts/optimization) 和 [引擎](/docs/concepts/engines)。

### 界面支持哪些语言？

界面支持 24 种语言。希伯来语是基础语言，布局支持从右到左的文本。希伯来语文档位于 [/docs/he](/docs/he)。

### Skynet 是开源的吗？

是的。代码使用 AGPL-3.0 许可证。如果你把修改后的 Skynet 作为服务提供，许可证要求你公开你的修改。

## 数据

### 我可以上传哪些文件类型？

你可以上传 CSV、JSON、`.xlsx` 和 `.xls` 文件。对于 Excel 文件，Skynet 只读取第一个工作表。请参阅[准备数据集](/docs/guides/datasets)。

### 我的数据没有标签，怎么办？

使用标注工具。你可以自己标注行，也可以让 AI 模型标注，而你来复核它的结果。请参阅[使用标注工具标注数据](/docs/guides/tagger)。

### 谁能看到我的运行和数据集？

> [!WARNING]
> 新运行默认是 **公开** 的。公开的运行会出现在公开搜索中，任何人都可以浏览其结果。

提交运行时，把 **可见性** 设为 **私有**，即可使其不出现在公开搜索中。新数据集默认是 **受限** 的：只有你和你邀请的人可以打开它们。请参阅[分享](/docs/guides/sharing)和[安全](/docs/security)。

### 模型提供商会用我的提示词训练吗？

默认不会。**模型数据隐私** 的默认选项是 **禁止训练**。它只会把提示词发送给不使用提示词训练、也不保留提示词的提供商。此设置适用于 Skynet 从你的余额中计费的调用。通过 OpenRouter 的 BYOK 调用遵循你的 OpenRouter 账户隐私设置。直接连接提供商的 BYOK 调用遵循该提供商的数据政策。请参阅[安全](/docs/security)。

## 计费

### Skynet 如何为运行收费？

Skynet 使用预付余额。托管模型用量及加价和沙箱用量均按量计费。充值时收取手续费。在付费工作开始之前，Skynet 会在你的钱包和运行预算中预留余量。请参阅[计费](/docs/concepts/billing)。

### 我可以使用自己的模型提供商密钥吗？

可以。使用自己的密钥（BYOK）时，提供商会将模型令牌费用计入你的密钥。Skynet 不收取模型费用。运行预算只涵盖沙箱用量。Skynet 会加密你的密钥，并且永远不会再次显示它。请参阅[计费](/docs/concepts/billing)和[安全](/docs/security)。

### 我可以在不为模型调用付费的情况下运行 Skynet 吗？

[Mac 应用](/docs/guides/mac-app)在你自己的电脑上运行 Skynet。它通过你在 Mac 上登录的 Claude Code 或 Codex 发送模型调用。这些调用由你的 CLI 套餐支付，因此不会给运行增加任何费用（$0）。

## 工具与集成

### 我可以从自己的 AI 客户端控制 Skynet 吗？

可以。Skynet 在 `https://mcp.skynetml.com/mcp` 提供 MCP 服务器。你通过 OAuth 登录。服务器提供以下工具：`check_run`、`start_run`、`get_run`、`list_runs` 和 `stop_run`。请参阅 [MCP](/docs/mcp)。

### 有 API 吗？

有。在“设置”中生成 API 令牌，然后以 `Authorization: Bearer <token>` 的形式发送。每个用户同一时间只有一个有效令牌。请参阅 [API](/docs/api)。

### 智能体面板可以删除我的运行吗？

只有在你允许时才可以。在默认的 **询问** 模式下，智能体在每个操作之前都会询问你。在 **自动** 模式下，它在执行破坏性操作（例如删除或提交）之前仍会询问你。请参阅[智能体面板](/docs/guides/agent-panel)。
