Esta página descreve como o Skynet protege a sua conta, as suas chaves e os seus dados. Ela também explica como relatar um problema de segurança.

## Relatar uma vulnerabilidade

Relate problemas de segurança de forma privada. Não abra uma issue pública.

- No GitHub, abra o repositório do Skynet e vá para **Security** > **Report a vulnerability**.
- Ou envie um e-mail para `security@skynetml.com`.

A equipe procura confirmar o recebimento de cada relato em até cinco dias úteis.

## A sua conta

### Métodos de login

Você pode entrar com:

- Um endereço de e-mail e uma senha.
- **Continuar com a Google** ou **Continuar com o GitHub**.
- **Iniciar sessão com uma passkey**. Uma passkey usa Face ID, Touch ID ou uma chave de hardware.

Você pode gerenciar os provedores que usa em **Contas associadas**.

### Regras de senha

Uma senha deve:

- Ter pelo menos 8 caracteres.
- Ter no máximo 128 caracteres.
- Não conter o seu endereço de e-mail.

Uma nova conta precisa verificar o endereço de e-mail com um código. Uma redefinição de senha também usa um código que o Skynet envia por e-mail.

O Skynet bloqueia o login de um endereço de e-mail por um período após repetidas tentativas sem sucesso.

### Verificação em duas etapas

Ative a verificação em duas etapas para ter mais proteção. Escolha um método:

| Método                         | Como funciona                                                       |
| ------------------------------ | ------------------------------------------------------------------- |
| **Aplicação de autenticação**  | Digite um código de 6 dígitos do seu app autenticador.              |
| **Códigos por e-mail**         | Digite um código de 6 dígitos que o Skynet envia a cada login.      |

O Skynet também fornece códigos de recuperação. Guarde-os em um lugar seguro. Use um deles se perder o acesso ao seu método.

> [!NOTE]
> Se você entra com Google ou GitHub, esse provedor gerencia a sua verificação em duas etapas.

## Onde o seu código é executado

As cargas de trabalho dos usuários rodam em sandboxes isoladas. No produto hospedado, a Vercel Sandbox é o limite de execução. Isso inclui verificações de configuração, execuções de otimização, recuperação e trabalho em execuções concluídas.

Os workers do Skynet não executam o seu código. Eles são o plano de controle. Eles iniciam jobs, reservam e liquidam orçamentos, fornecem credenciais com escopo restrito às sandboxes e salvam checkpoints.

Antes de iniciar um trabalho pago, o Skynet reserva uma margem na sua carteira e no orçamento da execução. Veja [Cobrança](/docs/concepts/billing).

No [app para Mac](/docs/guides/mac-app), os jobs rodam em contêineres Docker locais. Todos os serviços locais escutam apenas em `127.0.0.1`.

## Chaves de provedores (BYOK)

Você pode adicionar as suas próprias chaves de provedores de modelos.

- O Skynet criptografa cada chave em repouso.
- O Skynet nunca retorna a chave completa pela API.
- O Skynet guarda apenas os quatro últimos caracteres e um status: não verificada, verificada ou inválida.
- Quando você insere uma chave, o Skynet envia uma pequena requisição de teste para verificá-la.

O provedor cobra os tokens do modelo na sua chave. Skynet não cobra taxa pelo modelo. O orçamento da execução cobre apenas o uso da sandbox.

O Skynet também remove segredos de logs e mensagens de erro. Ele os substitui por `[REDACTED]`.

## Tokens de API

Os tokens de API permitem que scripts e ferramentas chamem a API do Skynet.

- Cada usuário tem no máximo um token válido. Quando você gera um novo token, o token anterior para de funcionar.
- Os tokens começam com `skyd_`.
- O Skynet armazena apenas um hash do token.
- O Skynet mostra o token completo uma única vez. Copie e guarde o token imediatamente.
- Depois disso, o Skynet mostra apenas uma forma mascarada: `skyd_…` e os quatro últimos caracteres.

Envie o token no cabeçalho `Authorization`:

```http
Authorization: Bearer skyd_...
```

Para invalidar um token, clique em **Revogar token**. Para a API completa, leia [API](/docs/api).

> [!WARNING]
> Trate o token como uma senha. Qualquer pessoa com o token pode agir como você pela API. Não faça commit dele em um repositório.

## Privacidade dos dados dos modelos

A configuração **Privacidade dos dados dos modelos** controla o que os provedores de modelos podem fazer com os seus dados. Ela vale para otimizações que o Skynet cobra do seu saldo.

| Opção               | Efeito                                                                                          |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| **Permitir todos**  | Qualquer provedor, incluindo provedores que podem treinar com os seus prompts ou armazená-los.  |
| **Sem treino**      | Apenas provedores que não treinam com os seus prompts e não os guardam. Esta é a opção padrão.  |
| **Sem retenção**    | Apenas endpoints com um acordo de retenção zero de dados. Alguns modelos podem ficar indisponíveis. |

> [!NOTE]
> Chamadas BYOK pelo OpenRouter seguem as configurações de privacidade da sua conta OpenRouter. Chamadas BYOK diretas seguem a política de dados do provedor.

## Visibilidade das execuções

> [!WARNING]
> Por padrão, novas execuções têm a visibilidade **Pública**. Uma execução pública aparece na pesquisa pública. Qualquer pessoa pode encontrá-la e navegar pelos resultados dela.

Para manter uma execução fora da pesquisa pública, defina **Visibilidade** como **Privada** ao enviá-la. Você pode alterar a visibilidade depois, na caixa de diálogo de compartilhamento. Veja [Compartilhamento](/docs/guides/sharing).

Novos conjuntos de dados têm o acesso **Restrito**. Só você e as pessoas que você convidar podem abri-los, até você alterar o acesso por link. Veja [Preparar um conjunto de dados](/docs/guides/datasets).

## Os seus dados

Abra a aba **Privacidade e dados** em **Definições** para gerenciar os seus dados.

### Exportar os seus dados

Clique em **Exportar** em **Exportar os meus dados**. A exportação contém todos os dados da sua conta. Escolha um formato: JSON, CSV, Excel, Pickle, Parquet ou Feather.

### Excluir a sua conta

Clique em **Eliminar conta**. Digite a sua senha atual e o seu endereço de e-mail para confirmar.

> [!WARNING]
> Isso exclui permanentemente a sua conta e todos os dados dela. Não é possível desfazer.

### Análises de uso

A configuração **Enviar análises de uso anônimas** ativa ou desativa as análises anônimas do produto.

## Próximos passos

- Leia [Limites](/docs/limits).
- Leia as [Perguntas frequentes](/docs/faq).
