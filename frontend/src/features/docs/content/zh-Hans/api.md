Skynet HTTP API 让脚本和工具能够做到 Web 应用能做的事：启动运行、观察运行并读取结果。本页介绍如何为请求签名、完整参考文档在哪里，并提供一个完整示例。

> [!TIP]
> 你在使用 Claude Code、Codex 或 Cursor 之类的 AI 智能体吗？请改为通过 [MCP](/docs/mcp) 连接它。这样智能体会替你启动和观察运行，并且每次运行都有支出上限。

## 基础 URL

把每个请求发送到：

```text
https://api.skynetml.com
```

## 身份验证

API 使用 API 令牌。

1. 在 Skynet 中打开 **设置**，然后打开 **API** 标签页。
2. 点击 **生成令牌**。
3. 立即复制令牌。Skynet 只会显示一次完整令牌。

在每个请求的 `Authorization` 请求头中发送令牌：

```http
Authorization: Bearer skyd_...
```

- 令牌以 `skyd_` 开头。
- 每个用户最多只有一个有效令牌。生成新令牌会使旧令牌失效。
- 点击 **吊销令牌** 可使令牌失效。

> [!WARNING]
> 像对待密码一样对待令牌。不要把它放进源代码和日志。关于 Skynet 如何存储令牌，请阅读[安全](/docs/security)。

## 交互式参考文档

完整参考文档位于 [api.skynetml.com/scalar](https://api.skynetml.com/scalar)。它列出了每个公开端点、其字段和响应。你可以在该页面发送测试请求。

参考文档读取的是位于 `https://api.skynetml.com/openapi.public.json` 的 OpenAPI 文档。你可以用这个文件为你的语言生成客户端。

## 主要端点

| 方法和路径                                      | 作用                                         |
| ----------------------------------------------- | -------------------------------------------- |
| `GET /models`                                   | 列出你可以使用的模型。                       |
| `GET /optimizers`                               | 列出适合某个设置的优化器。                   |
| `GET /blackbox/engines`                         | 列出原生引擎以及每个引擎是否可用。           |
| `POST /optimize` | 为 DSPy 程序启动一次运行。 |
| `POST /blackbox/run`                            | 在 GitHub 仓库上启动运行。                   |
| `GET /optimizations`                            | 列出你的运行。                               |
| `GET /optimizations/{optimization_id}`          | 返回一次运行：状态、进度、日志和结果。       |
| `GET /optimizations/{optimization_id}/artifact` | 返回运行找到的最佳版本。                     |
| `POST /optimizations/{optimization_id}/cancel`  | 停止一次运行。                               |
| `POST /serve/{optimization_id}`                 | 在新输入上运行最佳版本。                     |
| `GET /health`                                   | 显示服务是否正常运行。                       |

## 启动运行

`POST /optimize` 接受与提交向导相同的设置。示例会改进一个把客服消息归类到标签的 DSPy 程序。

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

重要字段：

- `task.artifact.kind` 取值为 `dspy_program`。
- `data.mode` 取值为 `pairs`（带期望输出的输入）或 `single`（恰好一个用例）。
- `optimizer.choice` 取值为 `auto`，或 `GET /optimizers` 返回的某个优化器名称。
- `max_cost_cents` 是本次运行的支出上限，单位为美分。
- `is_private` 默认为 `false`。设为 `true` 可将运行设为私有。

关于 `run_code` 和 `metric_code` 的规则，请阅读[指标与评分器](/docs/concepts/scorers)。

### 响应

| 状态码 | 含义                                                                                                          |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet 已将运行加入队列。响应体包含 `optimization_id` 和 `status`。                                           |
| `202`  | 设置检查仍在进行。等待 `Retry-After` 请求头中给出的秒数，然后用相同的 `Idempotency-Key` 发送相同的请求体。 |
| `402`  | 你的余额不足。                                                                                                |
| `409`  | 你达到了配额，或者设置检查已过期。                                                                            |
| `422`  | 请求或工作区代码与优化器不匹配。                                                                              |

> [!NOTE]
> 请始终发送 `Idempotency-Key` 请求头。如果你用相同的键再次发送相同的请求，Skynet 会返回第一次的运行，不会启动第二次运行。

## 观察运行

轮询运行，直到其状态变为最终状态：

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

`status` 字段的取值为以下之一：

| 状态                               | 最终状态 |
| ---------------------------------- | -------- |
| `pending`、`validating`、`running` | 否       |
| `paused`                           | 否       |
| `success`                          | 是       |
| `failed`、`cancelled`、`stopped`   | 是       |

每 10 到 30 秒轮询一次。一次运行可能需要几分钟到几小时。

[API 参考文档](https://api.skynetml.com/scalar)列出了运行响应的每个字段。

## 使用结果

当状态为 `success` 时，获取最佳版本：

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

要在新输入上运行最佳版本，请发送 `POST /serve/{optimization_id}`。关于每个分数的含义，请阅读[结果](/docs/guides/results)。

## 限制

API 使用与 Web 应用相同的配额、速率限制和计费方式。请阅读[限制](/docs/limits)和[计费](/docs/concepts/billing)。
