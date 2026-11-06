O avaliador diz ao Skynet quão boa é uma versão do seu repositório. Ele é uma função Python em `scorer.py`. Todos os motores chamam a mesma função. Esta página apresenta o contrato, o valor de retorno e exemplos.

## O contrato

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| Argumento   | O que contém                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | A pasta de uma versão do repositório, depois que o comando de configuração rodou |
| `case`      | Um dict. `case["input"]` contém os campos de entrada. `case["expected"]` contém o valor esperado, ou `None` |

Se a sua função receber apenas um argumento, o Skynet chama `score(repo_path)`. Use esta forma quando o avaliador roda os seus próprios testes e não precisa de casos.

O avaliador roda na sandbox, na pasta da versão. Ele pode importar código do repositório, executar um comando ou chamar `serve()` de `serve.py`. Leia [Como a otimização funciona](/docs/concepts/optimization).

## O valor de retorno

`score` deve retornar uma pontuação com feedback. Estes formatos são válidos:

1. Um par: um número e uma string de feedback ou um dict com uma chave `"feedback"`.
2. Um dict com um número em `"score"` e uma string em `"feedback"`.

O feedback deve ser uma string que não esteja vazia.

As informações adicionais também podem ter uma chave `"scores"`. Ela mapeia nomes para pontuações nomeadas. Cada pontuação nomeada tem a sua própria pontuação e o seu próprio feedback, por exemplo `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

As informações adicionais também podem ter uma chave `"cases"` com o mesmo formato. Cada entrada é o resultado de um caso. Todo motor usa cada caso como um objetivo próprio.

Se o valor de retorno tiver outro formato, a verificação falha e mostra o motivo.

| Valor de retorno                                                                | Válido                                       |
| ------------------------------------------------------------------------------- | -------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Sim                                          |
| `(0.8, "Close. Missing the date.")`                                             | Sim                                          |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Sim                               |
| `0.8`                                                                           | Não. Não há feedback.                        |
| `(0.8, {})`                                                                     | Não. Não há string de feedback.              |
| `("0.8", {"feedback": "OK"})`                                                   | Não. A pontuação é uma string.               |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | Não. Uma pontuação nomeada não é um número. |

## Escrever um feedback útil

O GEPA e o Auto leem o texto de feedback para escrever a próxima versão. Diga o que está errado e como é uma saída correta.

| Feedback fraco  | Feedback útil                                                                       |
| --------------- | ----------------------------------------------------------------------------------- |
| "Errado."       | "Esperado 'billing', obtido 'other'. A mensagem menciona uma cobrança."             |
| "Formato ruim." | "A saída não é um JSON válido. Há uma vírgula sobrando depois do último campo."     |

> [!NOTE]
> O agente de código nunca vê o código do seu avaliador, os valores esperados nem os segredos da execução. Ele vê apenas as pontuações e o texto de feedback.

## Exemplos

### Correspondência exata

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` é um dict indexado pelo nome da sua coluna de Saída. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

### Rodar os testes do repositório

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### Modelo como juiz

O avaliador pode chamar um modelo. Importe `llm` do módulo auxiliar `skynet`. A chamada passa pelo gateway de modelos do Skynet e vai para o modelo do avaliador da execução.

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
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

O avaliador também pode usar qualquer cliente compatível com OpenAI com as variáveis de ambiente `OPENAI_BASE_URL`, `APP_API_KEY` e `APP_MODEL`. O gateway aceita apenas o modelo do avaliador da execução.

## Chamadas de modelo e segredos

- As chamadas com as chaves do Skynet são medidas no orçamento da execução. Com a sua própria chave (BYOK), a chamada vai para a chave do seu provedor, sem taxa de modelo do Skynet. Leia [Cobrança](/docs/concepts/billing).
- Os segredos da execução chegam ao avaliador como variáveis de ambiente. O nome da variável é o nome do segredo, por exemplo `os.environ["SEARCH_API_KEY"]`.
- O Skynet oculta os valores dos segredos na saída do avaliador e nos logs.

## Limites

| Item                        | Valor                                  |
| --------------------------- | -------------------------------------- |
| Tempo limite do avaliador   | 60 segundos por padrão, no máximo 600  |

## Páginas relacionadas

- [Conjuntos de dados e divisões](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar uma execução](/docs/guides/submit)
