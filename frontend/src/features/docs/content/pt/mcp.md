O servidor MCP do Skynet permite que um agente de IA use o Skynet por você. Claude, ChatGPT, Codex, Cursor e outros clientes MCP podem verificar a configuração de uma execução, iniciar execuções, acompanhá-las e pará-las. Você aprova a conexão uma única vez no navegador. O agente define um limite de gastos para cada execução.

## URL do servidor

Todos os clientes usam a mesma URL:

```text
https://mcp.skynetml.com/mcp
```

O servidor usa Streamable HTTP. O login usa OAuth 2.1. Você não copia nenhum token nem chave de API para o cliente.

## Conectar o seu cliente

Selecione o seu cliente. Siga os passos dele e depois vá para [Aprovar a conexão](#aprovar-a-conexão).

```skynet-mcp-clients

```

## Aprovar a conexão

Na primeira vez que o agente chama o Skynet, o navegador abre uma página do Skynet.

1. Entre no Skynet, se ainda não tiver entrado.
2. Leia a página. Ela mostra o nome do agente, o site para onde ele volta e a sua conta.
3. Leia o que o agente pode fazer:
   - Ver as suas execuções de otimização e os resultados delas.
   - Iniciar e parar execuções de otimização, pagas com o seu saldo.
4. Clique em **Allow** para conectar. Clique em **Deny** para parar.

Depois que você clica em **Allow**, o navegador volta para o agente. Agora o agente pode chamar as ferramentas abaixo.

> [!NOTE]
> O agente define um limite de gastos para cada execução. Ele não pode adicionar fundos.

## Ferramentas

| Ferramenta  | O que faz                                                                                       | Acesso  |
| ----------- | ----------------------------------------------------------------------------------------------- | ------- |
| `check_run` | Verifica a configuração de uma execução sem executá-la nem cobrar você. Sugere um limite de gastos. | Leitura |
| `start_run` | Inicia uma execução com um limite de gastos.                                                    | Escrita |
| `get_run`   | Retorna o status, os gastos e, quando a execução termina, o resultado.                          | Leitura |
| `list_runs` | Lista as suas execuções, das mais recentes para as mais antigas. Pode filtrar por status.       | Leitura |
| `stop_run`  | Para uma execução. Uma segunda chamada retorna um erro que diz que a execução já terminou.      | Escrita |

O acesso de leitura usa o escopo `runs:read`. O acesso de escrita usa o escopo `runs:write`.

Nenhuma ferramenta adiciona fundos, altera o seu saldo ou altera as configurações de cobrança.

## Como um agente executa uma otimização

1. O agente chama `check_run` com a configuração da execução. O Skynet faz as verificações estáticas e retorna:
   - O resultado de cada verificação.
   - O menor limite de gastos com que a execução pode começar.
   - Um limite de gastos sugerido. Ele é de pelo menos US$ 5,00.
   - Os modelos que não têm preço conhecido.
   - O valor que você pode gastar agora.
2. O agente chama `start_run` com um limite de gastos (`max_cost_cents`, de 1 a 1.000.000 centavos de dólar americano). Ele também pode enviar uma `idempotency_key`, para que uma nova tentativa não inicie uma segunda execução.
3. Se a verificação da configuração ainda estiver em andamento, `start_run` retorna `in_progress` e `retry_after_seconds`. Depois desse tempo, o agente chama `start_run` de novo com a mesma entrada.
4. O agente chama `get_run` até a execução terminar. Os status finais são `success`, `failed`, `cancelled` e `stopped`.

> [!TIP]
> Informe ao agente o seu limite de gastos no prompt, por exemplo "Otimize este repositório. Gaste no máximo US$ 10." O agente então usa esse limite em `start_run`.

A execução para quando atinge o limite de gastos. Leia [Cobrança](/docs/concepts/billing) para saber como o Skynet cobra uma execução.

## Segurança

- Cada cliente recebe a sua própria concessão. O cliente nunca vê a sua senha.
- O login usa PKCE (S256). O cliente se registra por registro dinâmico de cliente ou por um documento de metadados de ID de cliente.
- Os tokens de acesso expiram depois de 1 hora. O cliente os renova com um token de atualização.
- Um token de atualização expira depois de 30 dias sem uso. Uma concessão dura no máximo 90 dias. Depois disso, você aprova a conexão de novo.
- Cada token de atualização funciona uma única vez. Se alguém usar de novo um token de atualização antigo, o Skynet encerra a concessão.
- O agente só pode ver e alterar as suas próprias execuções.

Leia [Segurança](/docs/security) para saber como o Skynet protege a sua conta e os seus dados.

## Desconectar

Para desconectar um agente, remova o servidor do Skynet do cliente. A maioria dos clientes então revoga os seus tokens. Se um cliente não os revogar, o token de atualização dele expira depois de 30 dias sem uso.

## Solução de problemas

| Problema                                          | O que fazer                                                                   |
| ------------------------------------------------- | ----------------------------------------------------------------------------- |
| A conexão falha imediatamente                     | Confira a URL do servidor. Ela deve ser exatamente `https://mcp.skynetml.com/mcp`. |
| "This agent can't connect"                        | Inicie a conexão de novo a partir do cliente.                                 |
| "Too many attempts"                               | Aguarde alguns minutos e tente de novo.                                       |
| O cliente não abre um navegador                   | Atualize o cliente. Ele precisa suportar servidores MCP remotos com OAuth.    |
| `start_run` falha com um erro de saldo            | Adicione fundos no Skynet ou inicie a execução com um limite de gastos menor. |
| As ferramentas param de funcionar após semanas    | A concessão expirou. Remova o servidor e conecte de novo.                     |

Para acesso HTTP direto sem um agente, leia [API](/docs/api).
