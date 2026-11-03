O painel do agente é um assistente de conversa dentro do Skynet. Ele pode ler as suas execuções, os seus conjuntos de dados e o seu saldo, e pode agir por você, por exemplo renomear ou cancelar uma execução. Você controla quais ações precisam da sua aprovação.

## Abrir o painel

O painel abre na lateral do app. Quando você o minimiza, um botão com o rótulo **Abrir o agente** fica na tela. Clique nele para abrir o painel de novo.

Clique em **O que posso fazer** para ver a lista das ferramentas do agente.

### Conversas

- **Nova conversa** inicia uma conversa vazia.
- **Conversas anteriores** lista as suas conversas mais antigas. Você pode pesquisá-las, fixá-las, renomeá-las e excluí-las.

### Enviar uma mensagem

Digite a sua mensagem e pressione Enter. Você também pode clicar em **Ditar** e falar a sua mensagem.

Enquanto o agente trabalha, o campo de mensagem continua disponível:

- **Enter** orienta o trabalho atual, quando suportado. O agente lê a mensagem no passo seguinte. **Tab** coloca um novo turno na fila. Sem orientação disponível, Enter também coloca a mensagem na fila.
- Em **Mensagens pendentes**, pode editar, eliminar e arrastar mensagens para alterar a ordem. **Orientar**, quando disponível, envia uma mensagem da fila para o trabalho atual.
- Uma mensagem marcada como **Não confirmado** pode já ter chegado ao agente. Quando estiver inativo, use o menu da mensagem para enviá-la explicitamente. Você pode editá-la ou excluí-la a qualquer momento. Outras mensagens na fila podem continuar antes dela.
- **Parar** termina o turno atual e coloca a fila em pausa. As instruções por ler regressam à lista. Nada começa automaticamente. **Esc** no campo faz o mesmo, exceto com um menu aberto ou durante a composição com IME. Esc fecha primeiro uma sugestão ou um menu deste campo.
- Sem um turno ativo, **Enviar agora** envia a primeira mensagem da fila em pausa. As restantes seguem pela ordem definida. O fim normal de um turno também inicia a mensagem seguinte.
- **Parar e enviar agora** para o turno e envia toda a lista num único novo turno. Aguarda o regresso das instruções por ler e junta-as antes das mensagens da fila, separadas por linhas em branco. Não reenvia instruções já lidas. As mensagens não confirmadas ficam na lista e não são incluídas. As outras continuam normalmente.
- O assistente de documentação e a conversa protegida dos programas disponibilizados usam **apenas a fila**. Enter e Tab colocam mensagens na fila. O assistente de documentação pausa as restantes após atingir um limite de pedidos.

## O que o agente pode fazer

O agente pode ler:

- As suas execuções, os logs delas e os resultados de teste delas.
- Análises e o saldo da sua carteira.
- Os conjuntos de dados da sua biblioteca e os resultados da pesquisa pública.
- As suas sessões de marcação (somente leitura).
- Os conjuntos de dados de exemplo, o catálogo de motores e o catálogo de otimizadores.

O agente também pode agir. As ações seguras incluem:

- Renomear e fixar execuções, e fixar muitas execuções de uma vez.
- Pausar uma execução.
- Editar e validar o código de uma execução.
- Analisar o perfil de um conjunto de dados e definir os papéis das colunas.
- Encontrar os modelos disponíveis.
- Atualizar o assistente de envio e as suas preferências.
- Preparar um conjunto de dados de exemplo.
- Criar uma pasta.

As ações destrutivas incluem:

- Excluir uma execução ou muitas execuções.
- Enviar, cancelar, clonar, tentar novamente, retomar ou reiniciar uma execução, e cancelar muitas execuções.
- Excluir, mover ou atualizar uma pasta, e mover execuções entre pastas.

> [!NOTE]
> As ferramentas web (pesquisar, buscar uma URL, rastrear um site) só estão disponíveis quando o servidor tem uma chave de pesquisa web configurada. Se você não as vê, a sua instância do Skynet não as oferece.

## Modos de confiança

O modo de confiança define quais ações precisam da sua aprovação. Clique no seletor de confiança no campo de mensagem para alterá-lo. O seletor alterna entre os três modos.

| Modo                  | Descrição                                     | Ações seguras        | Ações destrutivas    |
| --------------------- | --------------------------------------------- | -------------------- | -------------------- |
| **Perguntar**         | Pede confirmação antes de cada ação.          | Precisam de aprovação | Precisam de aprovação |
| **Automático**        | Realiza ações seguras sem confirmação.        | Rodam na hora        | Precisam de aprovação |
| **Sem restrições**    | Realiza tudo sem confirmação.                 | Rodam na hora        | Rodam na hora        |

O modo padrão é **Perguntar**. As ferramentas somente leitura nunca precisam de aprovação.

> [!WARNING]
> No modo **Sem restrições**, o agente pode excluir execuções ou enviar execuções pagas sem aprovação. Use este modo apenas quando confiar na tarefa.

### Definir o modo padrão

1. Abra **Definições**.
2. Abra a aba **Painel do agente**.
3. Em **Nível de confiança do agente**, escolha **Perguntar primeiro**, **Automático apenas para ações seguras** ou **Sem aprovação prévia**.

### Aprovar uma ação

Quando uma ação precisa de aprovação, o painel mostra um cartão de aprovação. O cartão tem um botão de confirmação para a ação e **Cancelar**. Clique em **Mostrar detalhes técnicos** para ver a chamada de ferramenta exata.

Se você não responder em 15 minutos, o agente trata a ação como recusada.

## Anexar um arquivo

Você pode anexar arquivos a uma mensagem. Cada arquivo pode ter até 20 MB. Você pode anexar um conjunto de dados por vez.

Para enviar um conjunto de dados pelo agente:

1. Anexe um arquivo CSV, JSON ou Excel.
2. Confira o cartão de envio. Ele mostra as colunas do arquivo.
3. Defina os papéis das colunas no cartão e depois confirme.

Se o agente não conseguir ler o arquivo, o cartão mostra "Não foi possível ler o ficheiro. Utilize um ficheiro CSV, JSON ou XLSX."

Para as regras de arquivo, veja [Preparar um conjunto de dados](/docs/guides/datasets).

## Escolher um modelo

O menu de modelos no campo de mensagem tem estes grupos:

- **Auto** deixa o Skynet escolher o modelo.
- **Créditos Skynet** usa um modelo hospedado, pago com o seu saldo.
- **Use a sua própria chave** usa um modelo pela sua própria chave de provedor.

Alguns modelos permitem definir o esforço de raciocínio. Os níveis são **Nenhum**, **Mínimo**, **Baixo**, **Médio**, **Elevado**, **Muito elevado** e **Máximo**. **Predefinição** usa o nível padrão do modelo.

Para definir um modelo padrão para novas conversas, abra **Definições** > **Painel do agente** > **Modelo padrão**.

### Custo

As conversas com o agente em créditos Skynet usam o seu saldo. Se o seu saldo estiver vazio, o painel mostra "O seu saldo está vazio. Adicione fundos para continuar a conversar com o agente." Para os preços, leia [Orçamentos e cobrança](/docs/concepts/billing).

## Dicas

> [!TIP]
> Peça ao agente para explicar uma execução antes de alterá-la. Por exemplo: "Por que a execução 42 teve uma pontuação menor que a execução 41?"

> [!TIP]
> Use o modo **Perguntar** quando experimentar um novo tipo de pedido. Mude para **Automático** quando souber o que o agente vai fazer.

## Outras formas de usar um agente

Você também pode conectar o seu próprio cliente de IA ao Skynet pelo servidor MCP. Veja [MCP](/docs/mcp). Para scripts, use a [API](/docs/api).

## Próximos passos

- [Inicie uma execução](/docs/guides/submit).
- [Leia os resultados](/docs/guides/results).
- Leia [Segurança](/docs/security) para saber como o Skynet protege as suas chaves.
