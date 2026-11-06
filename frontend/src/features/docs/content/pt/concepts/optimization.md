O Skynet é o Optimize Anything: um único produto que melhora um programa DSPy ou um repositório com base na sua métrica. O tipo de artefato decide quais motores podem trabalhar nele.

## O contrato comum

Toda execução tem dois arquivos. `run.py` executa o artefato em um caso. `metric.py` pontua a saída.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact` é a versão atual que o otimizador propõe.
- `input` é um dict que contém os campos de entrada de um caso.
- `expected` é o valor esperado do caso, ou `None` quando o caso não tem um.
- `score` retorna um par: um número e um dict com uma string `"feedback"`.

O módulo auxiliar `skynet` fornece `llm`, `Image` e `log`. Uma chamada a `llm` a partir do código de execução vai para o modelo da tarefa. Uma chamada a `llm` a partir da métrica vai para o modelo da métrica.

> [!NOTE]
> O código de execução roda no seu próprio processo na sandbox. Ele não vê o código da métrica, o valor esperado nem a chave da métrica.

Leia [Métricas e avaliadores](/docs/concepts/scorers) para ver o contrato completo da métrica.

## Tipos de artefato e motores

| Tipo de artefato | Motores                                                                      |
| ---------------- | ---------------------------------------------------------------------------- |
| Repositório | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| Programa DSPy    | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

Os motores se dividem em duas famílias:

- Os **motores nativos** tratam o artefato como uma caixa-preta. Eles trabalham com repositórios.
- Os **otimizadores DSPy** trabalham dentro de um programa DSPy. Eles trabalham apenas com artefatos de programa DSPy.

O DSPy MIPROv2 e o DSPy BootstrapFewShot precisam de pares (casos com saídas esperadas). Com uma única entrada, um programa DSPy só pode usar Auto e DSPy GEPA.

## Repositórios

Os motores nativos alteram os arquivos nos caminhos que você permite. Sua métrica pontua cada versão do repositório.

Os seis motores nativos usam métodos de busca diferentes:

- O **GEPA** evolui versões por reflexão e mantém uma fronteira de Pareto.
- O **Best-of-N** pede ao modelo de reflexão propostas independentes e mantém a melhor.
- O **AutoResearch**, o **Meta-Harness**, o **AutoSaddler** e o **ShinkaEvolve** usam um agente de programação para propor versões.

Leia [Motores](/docs/concepts/engines) para ver cada motor e as suas configurações.

## Artefatos de programa DSPy

Um programa DSPy é um tipo de artefato. O seu `run.py` cria um `dspy.Module` no nível do módulo e o chama dentro de `run`. Se o arquivo tiver mais de um módulo, chame de `program` aquele que deve ser otimizado.

O que acontece quando um otimizador DSPy é executado:

1. O Skynet encontra o seu módulo e envolve-o com a sua função `run`. Todo caso passa pela sua própria `run`.
2. O otimizador DSPy compila o programa nos casos de treino.
3. Ele seleciona com base nos casos de validação.
4. O vencedor é o texto de instrução de cada preditor, mais o estado compilado completo do módulo. O estado inclui as demonstrações few-shot.

As pontuações finais medem o mesmo programa que o Skynet salva.

| Otimizador            | Lê o texto de feedback |
| --------------------- | ---------------------- |
| DSPy GEPA             | Sim                    |
| DSPy MIPROv2          | Não, só a pontuação    |
| DSPy BootstrapFewShot | Não, só a pontuação    |

O DSPy BootstrapFewShot não usa um modelo de reflexão.

> [!TIP]
> O modelo de reflexão do DSPy vê apenas o texto de feedback que a sua métrica retorna. Ele nunca vê o código da métrica.

## Auto

O **Auto** é a escolha padrão no assistente.

- **Repositório**: O Auto não está disponível. Escolha um motor nativo.
- Em um programa DSPy, o Auto executa o DSPy GEPA.

O Auto lê o texto de feedback da sua métrica.

## Qual motor escolher

| A sua situação                                                    | Use                                                                    |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Você tem código em um repositório do GitHub | Um repositório. Escolha um motor nativo. |
| Você tem um programa DSPy                                         | Um artefato de programa DSPy. Comece com Auto.                         |
| Você quer demonstrações few-shot escolhidas a partir dos seus dados | Um artefato de programa DSPy com DSPy BootstrapFewShot ou DSPy MIPROv2 |

> [!WARNING]
> O GEPA, o DSPy GEPA e o Auto aprendem com o texto de feedback. Se o seu feedback for sempre a mesma string, esses otimizadores terão menos informação para trabalhar.

## Páginas relacionadas

- [Motores](/docs/concepts/engines)
- [Conjuntos de dados e divisões](/docs/concepts/datasets)
- [Métricas e avaliadores](/docs/concepts/scorers)
- [Iniciar uma execução](/docs/guides/submit)
