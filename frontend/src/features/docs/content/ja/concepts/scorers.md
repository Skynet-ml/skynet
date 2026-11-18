スコアラーは、リポジトリの 1 つのバージョンがどれだけ良いかを Skynet に伝えます。スコアラーは `scorer.py` 内の Python 関数です。すべてのエンジンが同じ関数を呼び出します。このページでは、コントラクト、戻り値、例を紹介します。

## コントラクト

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| 引数        | 保持する内容                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | セットアップコマンドの実行後の、リポジトリの 1 つのバージョンのフォルダー        |
| `case`      | dict。`case["input"]` は入力フィールドを保持します。`case["expected"]` は期待値、または `None` を保持します |

関数が引数を 1 つしか受け取らない場合、Skynet は `score(repo_path)` を呼び出します。スコアラーが独自のテストを実行し、ケースが不要な場合は、この形を使います。

スコアラーは、サンドボックス内のバージョンのフォルダーで実行されます。リポジトリのコードをインポートすることも、コマンドを実行することも、`serve.py` の `serve()` を呼び出すこともできます。[最適化の仕組み](/docs/concepts/optimization) をお読みください。

## 戻り値

`score` は 1 つのオブジェクトを返します。

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

- `"score"` は必須です。有限の数値です。バージョンの順位を決めるのはこのスコアだけです。
- `"feedback"` は任意のテキストです。GEPA と Auto はこれから学ぶので、付けることをお勧めします。フィードバックがないと、エンジンはスコアだけを見ます。実行ログはそのことを 1 回だけ伝えます。
- `"scores"` は任意です。名前を名前付きスコアに対応付けます。各名前付きスコアは、独自の `"score"` と任意の `"feedback"` を持つオブジェクトです。すべてのエンジンは、各名前付きスコアをそれぞれ独自の目的として使います。

ほかの形やキーはすべてエラーです。チェックは、誤った部分と有効な形を表示します。

| 戻り値 | 有効か |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | はい |
| `{"score": 0.8}` | はい。フィードバックはありません。 |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | はい |
| `0.8` | いいえ。値がオブジェクトではありません。 |
| `(0.8, "Close. Missing the date.")` | いいえ。値がオブジェクトではありません。 |
| `{"score": "0.8"}` | いいえ。スコアが文字列です。 |
| `{"score": 0.8, "scores": {"format": 1.0}}` | いいえ。名前付きスコアはオブジェクトである必要があります。 |
| `{"score": 0.8, "feedbak": "OK"}` | いいえ。キー `feedbak` は不明です。 |

## 役に立つフィードバックを書く

GEPA と Auto は、フィードバックの文章を読んで次のバージョンを書きます。何が間違っているか、正しい出力はどのようなものかを書いてください。

| 弱いフィードバック | 役に立つフィードバック                                                            |
| ------------------ | --------------------------------------------------------------------------------- |
| "Wrong."           | "Expected 'billing', got 'other'. The message mentions a charge."                 |
| "Bad format."      | "The output is not valid JSON. It has a trailing comma after the last field."     |

> [!NOTE]
> コーディングエージェントが、スコアラーのソース、期待値、実行シークレットを見ることはありません。見るのはスコアとフィードバックの文章だけです。

## 例

### 完全一致

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

`case["expected"]` は、出力列の名前をキーとする dict です。[データセットと分割](/docs/concepts/datasets) をお読みください。

### リポジトリのテストを実行する

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
```

### 審査員としてのモデル

スコアラーはモデルを呼び出せます。`skynet` ヘルパーモジュールから `llm` をインポートします。呼び出しは Skynet のモデルゲートウェイを通り、実行のスコアラーモデルに送られます。

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

スコアラーは、`OPENAI_BASE_URL`、`APP_API_KEY`、`APP_MODEL` の環境変数を使って、任意の OpenAI 互換クライアントを使うこともできます。ゲートウェイは、実行のスコアラーモデルだけを受け付けます。

## モデル呼び出しとシークレット

- Skynet のキーによる呼び出しは、実行予算に対して計測されます。独自のキー (BYOK) では、呼び出しはあなたのプロバイダーキーに送られ、Skynet のモデル料金はかかりません。[請求](/docs/concepts/billing) をお読みください。
- 実行シークレットは、環境変数としてスコアラーに届きます。変数名はシークレット名です。例: `os.environ["SEARCH_API_KEY"]`。
- Skynet は、スコアラーの出力とログの中のシークレットの値を隠します。

## 制限

| 項目                       | 値                                  |
| -------------------------- | ----------------------------------- |
| スコアラーのタイムアウト   | 既定で 60 秒、最大 600 秒           |

## 関連ページ

- [データセットと分割](/docs/concepts/datasets)
- [エンジン](/docs/concepts/engines)
- [実行を開始する](/docs/guides/submit)
