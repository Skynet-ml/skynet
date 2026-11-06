Um conjunto de dados é uma tabela de casos. Cada linha é um caso. O Skynet pontua cada versão do seu repositório nos casos com o seu avaliador e usa as pontuações para comparar versões. Esta página explica como o Skynet lê os casos, o que fazem os papéis das colunas e como ele divide os casos em conjuntos de treino, validação e teste.

## Carregar os dados

Na etapa **Entradas** do assistente, você tem duas opções:

- **Carregue um ficheiro CSV, JSON ou Excel**. O Skynet aceita arquivos `.csv`, `.json`, `.xlsx` e `.xls`.
- **Escolher da biblioteca**. Use um conjunto de dados que você salvou antes.

Uma execução pode ter no máximo 200.000 casos.

Leia [Conjuntos de dados](/docs/guides/datasets) para gerenciar conjuntos de dados salvos.

## Papéis das colunas

Em **Como ler os dados**, atribua um papel a cada coluna:

| Papel   | O que o Skynet faz com a coluna                                     |
| ------- | ------------------------------------------------------------------- |
| Entrada | Coloca o valor em `case["input"]`, que o seu avaliador recebe      |
| Saída   | Coloca o valor em `case["expected"]`, que o seu avaliador recebe              |
| Ignorar | Não usa a coluna                                                    |

Cada coluna também tem um tipo: **Texto** ou **Imagem**.

Só uma coluna pode ser a coluna de Saída. Se você definir uma segunda coluna como Saída, a primeira passa para Ignorar.

### O que o seu código recebe

Para cada linha, o Skynet monta um caso:

- `input` é um dict. Ele tem uma chave para cada coluna de Entrada.
- `expected` é um dict com uma chave: o nome da coluna de Saída.

Para esta tabela:

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

com os papéis `message` = Entrada, `label` = Saída e `id` = Ignorar, o seu código recebe:

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> No seu avaliador, leia o valor esperado pelo nome da coluna, por exemplo `case["expected"]["label"]`.

## Modos de dados

Os papéis definem o modo de dados da execução:

| Modo   | Quando                   | O que acontece                                                                                       |
| ------ | ------------------------ | ---------------------------------------------------------------------------------------------------- |
| Pares  | Uma coluna é de Saída    | Cada linha é um caso com uma entrada e um valor esperado. Você precisa de pelo menos 2 linhas.       |
| Único  | Nenhuma coluna é de Saída | A execução usa apenas as colunas de Entrada da primeira linha. É um caso sem valor esperado.        |

No modo único, o Skynet usa esse único caso para o treino, a seleção e o relatório. Nada fica reservado.

## A divisão

No modo de pares, o Skynet divide os casos em três conjuntos:

| Conjunto          | Para que serve                                                |
| ----------------- | ------------------------------------------------------------- |
| Treino            | O motor propõe e testa versões nestes casos.                  |
| Validação         | O Skynet seleciona a melhor versão nestes casos.              |
| Teste             | O Skynet informa a pontuação final nestes casos.              |

> [!NOTE]
> Os casos de teste nunca chegam a um motor, a um proponente ou à verificação do contrato. A pontuação final é calculada em casos que o otimizador não viu.

### A divisão padrão

| Número de pares | Treino | Validação | Teste |
| --------------- | ------ | --------- | ----- |
| 5 ou mais       | 70%    | 15%       | 15%   |
| Menos de 5      | 85%    | 15%       | 0%    |

Com menos de 5 pares, o Skynet não mantém um conjunto de teste.

### Como o Skynet conta os casos

O Skynet converte as frações em números inteiros de casos pelo método dos maiores restos. As contagens sempre somam o número de casos. Um conjunto com uma fração acima de zero recebe pelo menos um caso. Um conjunto com uma fração igual a zero não recebe nenhum caso.

Antes da divisão, o Skynet embaralha os casos. O embaralhamento usa a semente da execução. Se você não informar uma semente, o Skynet cria uma a partir dos casos, do objetivo e do artefato. Assim, se você enviar a mesma execução de novo, obterá a mesma divisão.

### Alternativas

- Com menos de 5 pares, o Skynet remove o conjunto de teste e reescala treino e validação para que somem 1.
- Se o treino estiver vazio, o Skynet usa os casos de validação como treino.
- Se não houver conjunto de validação, o Skynet seleciona nos casos de treino.
- Se não houver conjunto de teste, o Skynet informa a pontuação na validação. Se também não houver conjunto de validação, informa no treino.

> [!WARNING]
> Uma pontuação que o Skynet informa no treino ou na validação não é uma pontuação em dados reservados. Use pelo menos 5 pares para ter um conjunto de teste.

## Alterar a divisão

A seção de divisão mostra **Divisão recomendada**. Ela tem dois modos:

1. **Utilizar recomendação**: o Skynet usa a divisão padrão acima.
2. **Seleção manual**: você define **Treino**, **Validação** e **Teste** por conta própria.

Na seleção manual, cada campo aceita uma fração de 0 a 1, em passos de 0,05. Cada campo mostra o número de casos que recebe. As três frações devem somar 1. Se não somarem, o assistente mostra "A divisão tem de somar 1."

## Amostras do contrato

A verificação do contrato na etapa **Métrica e execução** executa a sua configuração e o seu avaliador na versão inicial, em 3 casos de amostra. O Skynet tira essas amostras apenas dos casos de treino e validação, nunca do teste.

## Páginas relacionadas

- [Conjuntos de dados](/docs/guides/datasets)
- [Métricas e avaliadores](/docs/concepts/scorers)
- [Iniciar uma execução](/docs/guides/submit)
- [Limites](/docs/limits)
