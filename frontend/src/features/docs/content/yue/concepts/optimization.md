Skynet 就係 Optimize Anything：每次運行都係最佳化一個倉庫。引擎會提出倉庫嘅新版本，你嘅評分器幫每個版本評分，Skynet 保留最好嗰個。

## 共用合約

每次運行都有一個倉庫同一個評分器。評分器係一個 Python 函數，佢會收到倉庫某個版本嘅路徑同一個案例。

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path` 係倉庫某個版本嘅資料夾，安裝指令已經喺入面運行過。
- `case` 係一個 dict。`case["input"]` 裝住一個案例嘅輸入欄位。`case["expected"]` 裝住預期值；如果案例冇預期值，就係 `None`。
- `score` 回傳一對值：一個數字，同一個包含 `"feedback"` 字串嘅 dict。

倉庫嘅根目錄有一個 `serve.py` 檔案，入面有 `def serve(input)`。評分器可以呼叫佢，運行完之後 Skynet 亦會用佢嚟提供最好嗰個版本。

> [!NOTE]
> 寫版本嘅編程 agent 睇唔到評分器原始碼、預期值，亦都睇唔到運行密鑰。

完整嘅評分器合約，請睇 [指標同評分器](/docs/concepts/scorers)。

## 你嘅程式碼發出嘅模型呼叫

你嘅倉庫程式碼同評分器可以經 Skynet 嘅模型閘道呼叫模型。沙盒會用環境變數提供一個兼容 OpenAI 嘅端點：

| 變數                 | 內容                                                  |
| -------------------- | ----------------------------------------------------- |
| `OPENAI_BASE_URL`    | 閘道 URL。`APP_GATEWAY_URL` 裝住同一個值。            |
| `APP_API_KEY`        | 只限今次運行用嘅 token。佢唔係供應商金鑰。            |
| `APP_MODEL`          | 要傳送嘅模型名稱。佢係今次運行嘅評分器模型。          |
| `ANTHROPIC_BASE_URL` | 畀 Anthropic SDK 用嘅閘道 URL。                       |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- 用 Skynet 金鑰嘅呼叫會計入運行預算。
- 用你自己嘅金鑰（BYOK）嘅話，呼叫會計落你嘅供應商金鑰。Skynet 唔收模型費用。
- 閘道會拒絕任何其他模型嘅請求。
- 運行密鑰會以環境變數嘅形式去到你嘅程式碼同評分器。變數名就係密鑰名。Skynet 會喺輸出入面隱藏密鑰嘅值。

沙盒冇其他網絡存取。

## 引擎

每個引擎都處理倉庫：

- **Auto** 係預設選擇。佢會試幾個引擎，然後由最好嗰個版本繼續行 GEPA。
- **GEPA** 用反思演化版本，並保留一個 Pareto 前沿。
- **Best-of-N** 叫反思模型提出獨立提案，然後保留最好嗰個。
- **AutoResearch**、**Meta-Harness**、**AutoSaddler** 同 **ShinkaEvolve** 用編程代理提出版本。

引擎只會改你准改嘅路徑入面嘅檔案。每個引擎同佢嘅設定，請睇 [引擎](/docs/concepts/engines)。

## Auto

**Auto** 係精靈入面每次運行嘅預設選擇。

1. Auto 用五個引擎探索：GEPA、AutoResearch、Meta-Harness、AutoSaddler 同 ShinkaEvolve。
2. 佢揀出呢啲引擎搵到最好嗰個版本。
3. 佢由嗰個版本繼續行 GEPA，直到預算用完。

Auto 會讀你評分器嘅回饋文字。

## 起始版本由邊度嚟

| 你嘅情況                     | 用                                                           |
| ---------------------------- | ------------------------------------------------------------ |
| 你嘅程式碼喺 GitHub 倉庫入面 | **用現有倉庫**。你揀嘅分支或者 commit 入面嘅程式碼就係起點。   |
| 你淨係有任務同數據           | **建立新倉庫**。Skynet 會寫第一個解決方案，嗰個就係起點。    |

> [!WARNING]
> GEPA 同 Auto 會由回饋文字學習。如果你嘅回饋永遠係同一句字串，呢啲優化器可以用嘅資訊就會少咗。

## 相關頁面

- [引擎](/docs/concepts/engines)
- [數據集同分割](/docs/concepts/datasets)
- [指標同評分器](/docs/concepts/scorers)
- [用精靈開始運行](/docs/guides/submit)
