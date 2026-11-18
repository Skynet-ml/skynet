O avaliador diz ao Skynet quão boa é uma versão do seu repositório. Ele é uma função Python em `scorer.py`. Todos os motores chamam a mesma função. Esta página apresenta o contrato, o valor de retorno e exemplos.

## O contrato

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argumento   | O que contém                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | A pasta de uma versão do repositório, depois que o comando de configuração rodou |
| `case`      | Um dict. `case["input"]` contém os campos de entrada. `case["expected"]` contém o valor esperado, ou `None` |

Se a sua função receber apenas um argumento, o Skynet chama `score(repo_path)`. Use esta forma quando o avaliador roda os seus próprios testes e não precisa de casos.

O avaliador roda na sandbox, na pasta da versão. Ele pode importar código do repositório, executar um comando ou chamar `serve()` de `serve.py`. Leia [Como a otimização funciona](/docs/concepts/optimization).

## O valor de retorno

`score` retorna um objeto:

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

- `"score"` é obrigatório. É um número finito. Só esta pontuação ordena as versões.
- `"feedback"` é um texto opcional. Ele é recomendado, porque GEPA e Auto aprendem com ele. Sem feedback, os motores veem só as pontuações, e o log da execução avisa isso uma vez.
- `"scores"` é opcional. Ele mapeia nomes para pontuações nomeadas. Cada pontuação nomeada é um objeto com o seu próprio `"score"` e um `"feedback"` opcional. Todo motor usa cada pontuação nomeada como um objetivo próprio.

Qualquer outro formato ou chave é um erro. A verificação mostra a parte errada e o formato válido.

| Valor de retorno | Válido |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Sim |
| `{"score": 0.8}` | Sim. Não há feedback. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Sim |
| `0.8` | Não. O valor não é um objeto. |
| `(0.8, "Close. Missing the date.")` | Não. O valor não é um objeto. |
| `{"score": "0.8"}` | Não. A pontuação é uma string. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | Não. Uma pontuação nomeada deve ser um objeto. |
| `{"score": 0.8, "feedbak": "OK"}` | Não. A chave `feedbak` é desconhecida. |

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
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` é um dict indexado pelo nome da sua coluna de Saída. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

### Rodar os testes do repositório

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
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
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
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
