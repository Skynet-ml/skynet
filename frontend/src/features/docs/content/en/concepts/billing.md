Skynet uses a prepaid balance. You add funds to the balance. Each run has a spending limit, and Skynet charges the run's real usage to your balance. This page explains the balance, run budgets, reservations, fees and your own provider keys.

> [!NOTE]
> The numbers on this page are the current defaults. The app shows the live values. Open **Settings → Billing** to see your balance, the top-up fee and your transaction history.

## Balance

Your balance is in US dollars. Skynet keeps it in cents.

Skynet gives no free starting balance by default. Add funds before you start your first paid run.

You add funds in **Settings → Billing**. You can pick a fixed amount or enter a custom amount:

| Top-up        | Amount added to your balance |
| ------------- | ---------------------------- |
| Fixed amounts | $5, $20 or $50               |
| Custom        | Any amount from $5 to $1,000 |

The amount you choose is the amount that goes into your balance. The top-up fee comes on top of it. See the fees below.

**Settings → Usage** shows what you spent. The transaction history in **Settings → Billing** lists your top-ups and run charges.

## Run budgets

Each run has a spending limit. The limit is the most the run may spend. It covers setup and optimization.

A run has no cost estimate before it starts. It spends up to its limit, stops at the budget, and keeps its best version. Read [Runs](/docs/concepts/runs) to see how Skynet decides when to stop.

You set the limit on the **Budget** screen of the submit wizard, in the **Spending limit** field:

- **Set a limit** is the default mode. The limit starts at $5. If the run needs more than $5 to start, the limit starts at that amount.
- **No limit** lets the run draw on your balance until it finishes. The run never pauses. If your balance runs out, the run stops and keeps what it produced.

### The amount needed to start

The wizard shows the amount **Needed to start**. This amount is:

- the holds for the sandboxes that the run opens before its first model call, plus
- spare room for the first model calls. The spare room is $1, or the cost of the most expensive single call of your selected models if that is more.

The wizard does not let you continue when the limit is below this amount. It also stops you when your balance is below this amount. Raise the limit or add funds to continue.

> [!TIP]
> When a run stops at its budget, use **Raise limit and continue** on the run page. The run continues from its last checkpoint.

## Reservations

Before Skynet does paid work, it reserves part of the run's limit. Skynet releases the reservation when the work settles and charges only the real cost.

When a budget already has activity, the **Spending limit** card in the wizard shows its ledger:

| Label                       | Meaning                                                                    |
| --------------------------- | -------------------------------------------------------------------------- |
| **Remaining in this limit** | The part of the limit that is left.                                        |
| **Spent so far**            | The cost that settled.                                                     |
| **Reserved**                | Money held for work in progress. Skynet releases it when the work settles. |

## Fees

Skynet has two charges on top of the provider cost.

| Charge                        | Current default                      | Applies to                                        |
| ----------------------------- | ------------------------------------ | ------------------------------------------------- |
| Top-up fee                    | 12.5% of the amount, plus $0.35      | Each top-up                                       |
| Usage markup                  | Provider cost × 1.15 (cost plus 15%) | Model calls on Skynet's keys, and sandbox compute |

### Top-up fee

Skynet rounds the percentage part up to the next cent and then adds $0.35. For example, a $20 top-up has a fee of $2.85. The top-up screen shows the fee before you pay.

### Usage markup

Skynet multiplies the provider cost by the usage markup. The markup applies to model calls that Skynet pays for, and to sandbox compute. Reservations, the amount needed to start, and the model prices in the app already include the markup.

Skynet finds the provider cost of a model call in this order:

1. The cost that OpenRouter reports for the call.
2. OpenRouter's live price list for the model.
3. The LiteLLM price table.
4. A high fallback rate of $15 per million input tokens and $75 per million output tokens. This rate makes sure that Skynet never charges an unknown model below its cost.

## Your own provider keys (BYOK)

You can run model calls on your own provider key. Add keys in **Settings → Providers**. For OpenRouter, you can also use **Continue with OpenRouter**.

When a model call uses your key:

- Your provider bills the model tokens to your key.
- Skynet charges no model fee.
- The run budget covers sandbox usage only.

Any fees from an external scorer or MCP provider go directly to that provider. They are not part of the run's spending limit.

## Sandbox usage

Your run code and metric run in a sandbox. Skynet meters sandbox compute:

1. Before a sandbox starts, Skynet reserves an amount from the run's limit.
2. When the sandbox finishes, Skynet charges the final usage that the sandbox provider reports.
3. The usage markup applies to sandbox compute.

Sandbox compute is part of the run's spending limit, also when the model calls use your own key.

## Plans

There are two plans: Free and Pro. Pro raises some limits, such as concurrent runs and storage. If a Pro renewal fails, the account keeps Pro limits for 7 days. After that, it uses the Free limits until the payment succeeds. See [Limits](/docs/limits).

## Platform safeguards

- An interactive model turn, such as a chat with the assistant, needs a balance of at least $0.05 before it starts.
- The wizard assistant is free. Each account can start 50 wizard assistant turns per UTC day.

## Related pages

- [Runs](/docs/concepts/runs)
- [Start a run with the wizard](/docs/guides/submit)
- [Limits](/docs/limits)
