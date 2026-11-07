O assistente de envio configura e inicia uma execução. Ele tem quatro etapas: **Tarefa**, **Entradas**, **Otimizador** e **Métrica e execução**. Cada etapa tem uma ou mais telas. Este guia explica cada tela, da tarefa à verificação e ao início da execução.

> [!NOTE]
> O assistente mostra mais ou menos configurações conforme o nível de experiência: **Guiado**, **Padrão** ou **Especialista**. Guiado é o padrão. Altere o nível em **Definições → Conta**, em **Quanto mostrar**. Alguns rótulos são mais simples no nível Guiado. Este guia usa os rótulos dos níveis Padrão e Especialista e indica entre parênteses o rótulo do nível Guiado quando ele é diferente.

## Etapa 1: Tarefa

### O que quer otimizar?

Descreva a sua tarefa ao assistente na conversa **Entrevista sobre a tarefa**. O assistente preenche dois campos para você:

- **Objetivo** (Guiado: **Meta**): o que o código otimizado precisa fazer bem.
- **Contexto** (Guiado: **Contexto adicional**): contexto, restrições e como é um bom resultado.

Você pode editar os dois campos. O assistente não sobrescreve as suas edições.

### De onde vem o código

O Skynet é o Optimize Anything: cada execução otimiza um repositório. Selecione uma de duas opções:

- **Usar um repositório**: otimize código em um repositório GitHub que você conectou.
- **Criar um repositório novo**: o Skynet cria um novo repositório GitHub na sua conta. O assistente escreve uma primeira solução a partir da sua tarefa e dos seus dados. Essa solução é a versão inicial.

Conecte o GitHub em **Definições → Conectores** antes de começar.

### Usar um repositório

Defina estes campos:

- **Repositório**: escolha um repositório ou escreva-o como `owner/name`.
- **Commit**: um SHA de 40 caracteres. Deixe vazio para usar o commit mais recente do branch.
- **Caminhos que o otimizador pode alterar**: um arquivo ou uma pasta por linha. Um ponto significa o repositório inteiro.
- **Comando de configuração**: é executado em cada checkout novo antes da pontuação. Deixe vazio e o Skynet encontra o comando.

### Criar um repositório novo

Defina estes campos:

- **Nome**: o nome do novo repositório. A sua conta GitHub não pode já ter um repositório com esse nome.
- **Privado** ou **Público**: quem pode ver o repositório no GitHub.

O Skynet envia cada candidato para o seu próprio branch. O branch `skynet/best` aponta para a melhor versão.

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

- **Modelo de reflexão**: o modelo que lê as pontuações e o feedback e escreve os próximos candidatos. Com um otimizador baseado em agente de programação, este é o modelo do agente.
- **Modelo da tarefa**: o modelo que o código do seu repositório e o seu avaliador podem chamar pelo gateway de modelos do Skynet. Leia [Como a otimização funciona](/docs/concepts/optimization).

### Orçamento

Defina por quanto tempo a execução pode continuar tentando e quanto pode gastar:

- **Máx. de execuções do avaliador** (Guiado: **Máx. de tentativas**): o número máximo de candidatos que o Skynet pontua. Alguns otimizadores precisam de um número mínimo.
- **Parar na pontuação** (Guiado: **Parar mais cedo quando a pontuação chegar a**): a execução para quando uma versão atinge essa pontuação.
- **Máx. de iterações** (Guiado: **Máx. de rondas**): aparece para os otimizadores que suportam essa configuração.
- **Limite de gastos**: o máximo que a execução pode gastar. O padrão é US$ 5, ou o valor **Necessário para começar**, se for maior. Você também pode selecionar **Sem limite**.

O assistente não deixa você continuar se o limite ou o seu saldo estiver abaixo do valor necessário para começar. Leia [Orçamentos e cobrança](/docs/concepts/billing).

## Etapa 4: Métrica e execução

### Avaliador

Escreva `scorer.py`. Ele define `score(repo_path, case)` e retorna uma pontuação com feedback. O assistente pode escrevê-lo para você. Leia [Métricas e avaliadores](/docs/concepts/scorers).

O repositório pode ter um arquivo `serve.py` com `def serve(input)`. O Skynet o usa para servir a melhor versão depois da execução. Leia [Ler os resultados](/docs/guides/results).

### Segredos

Adicione segredos, como chaves de API de que o seu código precisa. Cada segredo chega ao seu código e ao seu avaliador como uma variável de ambiente com o mesmo nome. O agente de programação não pode vê-los. O Skynet oculta os valores deles nos registos.

### Verificação

A verificação (Guiado: **Execução de teste**) executa a configuração e o seu avaliador na versão inicial, na sandbox. Ela verifica:

- a assinatura do avaliador,
- o comando de configuração,
- o valor devolvido pelo avaliador nos casos de amostra.

Selecione **Verificar** (Guiado: **Testar**). Você não pode iniciar a execução até a verificação passar. Se você alterar o avaliador ou a configuração depois de uma verificação aprovada, execute a verificação de novo.

> [!TIP]
> Abra cada amostra para ver a pontuação e o feedback. Um bom feedback dá ao otimizador mais material de trabalho do que a pontuação sozinha.

### Execução agendada

Você pode executar a otimização de novo de forma agendada depois que ela for bem-sucedida. Cada ciclo agendado começa a partir da melhor versão atual do repositório. O padrão é **Desativada**.

### Pronto para executar

A última tela mostra um resumo em cinco abas: **Geral**, **Dados**, **Modelos**, **Orçamento** e **Recompensa**. Confira a configuração e inicie a otimização.

O resumo também mostra a visibilidade da execução. Por padrão, novas execuções são públicas. Altere isso depois na caixa de diálogo **Partilha e privacidade** da página da execução. Leia [Compartilhar e publicar](/docs/guides/sharing).

## Depois de iniciar

A página da execução é aberta. Leia [Execuções](/docs/concepts/runs) e [Ler os resultados](/docs/guides/results).
