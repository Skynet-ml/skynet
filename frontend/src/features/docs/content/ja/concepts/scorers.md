スコアラーは、リポジトリの 1 つのバージョンがどれだけ良いかを Skynet に伝えます。スコアラーは `scorer.py` 内の Python 関数です。すべてのエンジンが同じ関数を呼び出します。このページでは、コントラクト、戻り値、例を紹介します。

## コントラクト

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| 引数        | 保持する内容                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | メトリクスファイルだけを保持するフォルダー。ほかのリポジトリのファイルとバージョンのコードは保持しません |
| `case`      | dict。`case["input"]` は入力フィールドを保持します。`case["expected"]` は期待値、または `None` を保持します |
| `output`    | `[run]` エントリーがそのケースに対して返した値。ファイルに `[run]` がない場合は `None` |

リポジトリの `optimize.toml` ファイルには、`[run]` セクションまたは `[target.<name>]` ステージが必要です。`[run]` の `entry` は、1 つのバージョンを 1 つのケースで、専用のサンドボックスで実行します。どちらもないファイルは Skynet が拒否します。すべてのバージョンが同じスコアになるからです。

- 関数エントリー（例: `entry = "serve.py:serve"`）は、`case["input"]` を引数として受け取ります。`output` はその戻り値です。
- コマンドエントリー（例: `entry = ["python", "-m", "pytest", "-q"]`）は、入力を JSON として stdin で受け取ります。`output` は `{"exit_code": ..., "stdout": "..."}` です。

スコアラーは別のサンドボックスで実行されます。そのサンドボックスはメトリクスファイルだけを保持します。スコアラーはバージョンのコードをインポートすることも、実行することもできません。スコアラーには `output` を通してだけ、または `[target.<name>]` ステージへの `call()` を通してだけバージョンが見えます。`call` は `from openascent import call` でインポートします。[最適化の仕組み](/docs/concepts/optimization) をお読みください。

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

```toml tab="optimize.toml"
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

`case["expected"]` は、出力列の名前をキーとする dict です。[データセットと分割](/docs/concepts/datasets) をお読みください。

### リポジトリのテストを実行する

`[run]` エントリーがテストを実行します。スコアラーは終了コードとテストの出力を読みます。

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["stdout"][-2000:]}
```

`[run]` エントリーがあるケースで失敗すると、そのケースにはスコアがありません。エントリーが起動できない、タイムアウトする、シグナルで止まる、または関数エントリーが値を返す前に例外を送出するか終了すると、フィードバックは `Run failed: …` になり、Skynet はスコアラーを呼び出しません。コマンドエントリーの 0 以外の終了コードは失敗ではありません。スコアラーは `output` でそれを受け取ります。そのようなケースを持つバージョンにはスコアがありません。そのバージョンは最良のバージョンにならず、どの `[outcome] target` にも届かず、pull request も開きません。失敗を数値として数える必要がある場合は、プログラムの中で失敗を捕捉し、出力を書き、スコアラーにその出力を採点させてください。[リポジトリのルール](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md) をお読みください。

### 審査員としてのモデル

スコアラーはモデルを呼び出せます。`openascent` ヘルパーモジュールから `llm` をインポートします。呼び出しは Skynet のモデルゲートウェイを通り、実行のスコアラーモデルに送られます。

```toml tab="optimize.toml"
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

スコアラーは、`OPENAI_BASE_URL`、`APP_API_KEY`、`APP_MODEL` の環境変数を使って、任意の OpenAI 互換クライアントを使うこともできます。ゲートウェイは、実行のスコアラーモデルだけを受け付けます。

## モデル呼び出しとシークレット

- Skynet のキーによる呼び出しは、実行予算に対して計測されます。独自のキー (BYOK) では、呼び出しはあなたのプロバイダーキーに送られ、Skynet のモデル料金はかかりません。[請求](/docs/concepts/billing) をお読みください。
- スコアラーは、実行シークレットを環境変数として受け取りません。シークレットがスコアラーに届くのは、`[metric] services` が挙げる `[services.<name>]` テーブルか、`[metric] mcp` ファイルにあるサーバーを通る場合だけです。シークレットは、テーブルの `headers` に `${NAME}` として書きます。スコアラーは、`ASCENT_SERVICE_<NAME>_URL` の URL にリクエストを送ります。Skynet はサンドボックスの外でシークレットを追加します。
- Skynet は、スコアラーの出力とログの中のシークレットの値を隠します。

## 制限

| 項目                       | 値                                  |
| -------------------------- | ----------------------------------- |
| スコアラーのタイムアウト   | 既定で 60 秒、最大 600 秒           |

## 関連ページ

- [データセットと分割](/docs/concepts/datasets)
- [エンジン](/docs/concepts/engines)
- [実行を開始する](/docs/guides/submit)
