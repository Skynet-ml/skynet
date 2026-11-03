O assistente de envio configura e inicia uma execução. Ele tem quatro etapas: **Tarefa**, **Entradas**, **Otimizador** e **Métrica e execução**. Cada etapa tem uma ou mais telas. Este guia explica cada tela, da tarefa à verificação do contrato e ao início da execução.

> [!NOTE]
> O assistente mostra mais ou menos configurações conforme o nível de experiência: **Guiado**, **Padrão** ou **Especialista**. Guiado é o padrão. Altere o nível em **Definições → Conta**, em **Quanto mostrar**. Alguns rótulos são mais simples no nível Guiado. Este guia usa os rótulos dos níveis Padrão e Especialista e indica entre parênteses o rótulo do nível Guiado quando ele é diferente.

## Etapa 1: Tarefa

### O que quer otimizar?

Descreva a sua tarefa ao assistente na conversa **Entrevista sobre a tarefa**. O assistente preenche dois campos para você:

- **Objetivo** (Guiado: **Meta**): o que o artefato otimizado precisa fazer bem.
- **Contexto** (Guiado: **Contexto adicional**): contexto, restrições e como é um bom resultado.

Você pode editar os dois campos. O assistente não sobrescreve as suas edições.

### O que será otimizado

Selecione o **Tipo de artefacto**:

| Tipo de artefato | O que muda                                            |
| ---------------- | ----------------------------------------------------- |
| Prompt           | Instruções enviadas a um modelo de linguagem          |
| Código           | O código-fonte de um programa                         |
| Programa DSPy    | As instruções dos preditores de um módulo DSPy        |
| Texto            | Qualquer outro texto, como um documento ou um modelo  |

Depois informe a **Versão inicial** (Guiado: **De onde partimos**). Cole a versão atual ou deixe o assistente escrever um rascunho.

### Otimizar um repositório

Você também pode otimizar código em um repositório. Defina estes campos:

- **Repositório**: escolha um repositório ou escreva-o como `owner/name`.
- **Commit**: um SHA de 40 caracteres. Deixe vazio para usar o commit mais recente do branch.
- **Caminhos que o otimizador pode alterar**: um arquivo ou uma pasta por linha. Um ponto significa o repositório inteiro.
- **Comando de configuração**: é executado em cada checkout novo antes da pontuação. Deixe vazio e o Skynet encontra o comando.

## Etapa 2: Entradas

### Dados

Adicione os casos da execução. Você pode:

- Enviar um arquivo CSV, JSON ou Excel.
- Escolher um conjunto de dados da sua biblioteca.

Leia [Conjuntos de dados e divisões](/docs/concepts/datasets) e [Preparar um conjunto de dados](/docs/guides/datasets).

### Como ler os dados

O assistente lê os dados e sugere como usá-los. Confira as escolhas dele:

- Marque cada coluna como entrada, como saída esperada ou ignore-a.
- Sem coluna de saída, o Skynet usa a primeira linha como uma única entrada.
- Defina a divisão em conjuntos de treino, validação e teste. A divisão deve somar 1.

> [!WARNING]
> Você não pode abrir as etapas seguintes enquanto a etapa **Entradas** tiver um problema. Corrija os problemas que o assistente mostra e depois continue.

## Etapa 3: Otimizador

No nível Padrão, esta etapa tem uma tela. Ela mostra o otimizador, o orçamento e, quando necessário, os modelos. Nos níveis Guiado e Especialista, a etapa tem três telas: **Otimizador**, **Modelos** e **Orçamento**.

### Otimizador

O assistente lista apenas os otimizadores adequados à sua tarefa e às suas entradas. O **Auto** já vem selecionado. Selecione outro otimizador se quiser. Leia [Motores](/docs/concepts/engines) e [Como a otimização funciona](/docs/concepts/optimization).

### Modelos

- **Modelo da tarefa**: o modelo que o seu código de execução chama. Deixe vazio se o seu código de execução não chamar um modelo. Um programa DSPy precisa ter um modelo da tarefa.
- **Modelo de reflexão**: o modelo que lê as pontuações e o feedback e escreve os próximos candidatos. Com um otimizador baseado em agente de programação, este é o modelo do agente.

### Orçamento

Defina por quanto tempo a execução pode continuar tentando e quanto pode gastar:

- **Máx. de execuções do avaliador** (Guiado: **Máx. de tentativas**): o número máximo de candidatos que o Skynet pontua. Alguns otimizadores precisam de um número mínimo.
- **Parar na pontuação** (Guiado: **Parar mais cedo quando a pontuação chegar a**): a execução para quando uma versão atinge essa pontuação.
- **Máx. de iterações** (Guiado: **Máx. de rondas**): aparece para os otimizadores que suportam essa configuração.
- **Limite de gastos**: o máximo que a execução pode gastar. O padrão é US$ 5, ou o valor **Necessário para começar**, se for maior. Você também pode selecionar **Sem limite**.

O assistente não deixa você continuar se o limite ou o seu saldo estiver abaixo do valor necessário para começar. Leia [Orçamentos e cobrança](/docs/concepts/billing).

## Etapa 4: Métrica e execução

### Código e métrica

Escreva dois arquivos:

- `run.py`: o código de execução que executa o seu artefato em um caso.
- `metric.py`: a métrica que pontua a saída e escreve o feedback.

O **Agente de programação** pode escrever e alterar qualquer arquivo para você. Ele verifica o próprio código e corrige o que falha. Você pode adicionar até 20 arquivos extras. Leia [Métricas e avaliadores](/docs/concepts/scorers).

A métrica define seu modelo na própria chamada a `llm()`. Sem `model=`, a métrica usa o modelo de reflexão.

### Verificação do contrato

A **Verificação do contrato** (Guiado: **Execução de teste**) executa o seu código e a sua métrica na sandbox em 3 casos de amostra. Ela verifica:

- a assinatura do código de execução,
- a assinatura da métrica,
- o modelo da métrica,
- o código de execução nas amostras,
- o valor devolvido pela métrica.

Selecione **Verificar** (Guiado: **Testar**). Você não pode iniciar a execução até a verificação passar. Se você alterar o código ou a configuração depois de uma verificação aprovada, execute a verificação de novo.

> [!TIP]
> Abra cada amostra para ver a entrada, a saída e o feedback. Um bom feedback dá ao otimizador mais material de trabalho do que a pontuação sozinha.

### Execução agendada

Você pode executar a otimização de novo de forma agendada depois que ela for bem-sucedida. O padrão é **Desativada**.

### Pronto para executar

A última tela mostra um resumo em cinco abas: **Geral**, **Dados**, **Modelos**, **Orçamento** e **Recompensa**. Confira a configuração e inicie a otimização.

O resumo também mostra a visibilidade da execução. Por padrão, novas execuções são públicas. Altere isso depois na caixa de diálogo **Partilha e privacidade** da página da execução. Leia [Compartilhar e publicar](/docs/guides/sharing).

> [!NOTE]
> Uma execução de repositório tem apenas a tela **Código e métrica** e o resumo nesta etapa. O Skynet verifica a configuração com o repositório quando você inicia a execução.

## Depois de iniciar

A página da execução é aberta. Leia [Execuções](/docs/concepts/runs) e [Ler os resultados](/docs/guides/results).
