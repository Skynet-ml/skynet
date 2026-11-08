The agent panel is a chat assistant inside Skynet. It can read your runs, datasets and balance, and it can act for you, such as rename or cancel a run. You control which actions need your approval.

## Open the panel

The panel opens at the side of the app. When you minimize it, a pill with the label **Open the agent** stays on the screen. Click the pill to open the panel again.

Click **What I can do** to see a list of the agent's tools.

### Conversations

- **New conversation** starts an empty chat.
- **Past conversations** lists your older chats. You can search, pin, rename and delete them.

### Send a message

Type your message and press Enter. You can also click **Dictate** and speak your message.

While the agent works, the composer stays available:

- **Enter** steers the current work where supported. The agent reads the message at its next step. **Tab** queues a new turn. Where steering is unavailable, Enter also queues.
- The **Pending messages** tray lets you edit, delete and drag messages to reorder them. **Steer**, when available, moves a queued message into the current work.
- A message marked **Not confirmed** may already have reached the agent. It stays in the tray. Edit it to send it again, or delete it. Other queued messages can continue first.
- A steer shows in the chat as your message right away. If the turn ends before the agent reads it, it runs as the next turn.
- **Stop** stops the current turn. Unread steers and queued messages return to the composer, in order, after any text you already typed. Nothing starts until you send again. **Esc** in the composer does the same. It first dismisses a composer suggestion or menu, and does not stop during IME composition. A failed turn also returns them to the composer.
- A normal turn completion starts the next queued message.
- When signed in, the docs assistant accepts **Enter** as steering after it announces a safe step boundary. It reads the message at the next existing model step. **Tab** always queues. Before that announcement, when signed out, and in protected served chat, Enter and Tab both queue. After a rate limit, the docs assistant returns remaining messages to the composer.

## What the agent can do

The agent can read:

- Your runs, their logs and their test results.
- Analytics and your wallet balance.
- Your library datasets and public search results.
- Your tagging sessions (read only).
- The sample datasets, the engine catalog and the optimizer catalog.

The agent can also act. Safe actions include:

- Rename and pin runs, and pin many runs at once.
- Pause a run.
- Edit and validate the code of a run.
- Profile a dataset and set its column roles.
- Find available models.
- Update the submit wizard and your preferences.
- Stage a sample dataset.
- Create a folder.

Destructive actions include:

- Delete one run or many runs.
- Submit, cancel, clone, retry, resume or restart a run, and cancel many runs.
- Delete, move or update a folder, and move runs between folders.

> [!NOTE]
> Web tools (search, fetch a URL, crawl a site) are available only when the server has a web search key set up. If you do not see them, your Skynet instance does not offer them.

## Trust modes

The trust mode sets which actions need your approval. Click the trust toggle in the composer to change it. The toggle cycles through the three modes.

| Mode             | Description                                 | Safe actions  | Destructive actions |
| ---------------- | ------------------------------------------- | ------------- | ------------------- |
| **Ask**          | Asks for confirmation before every action.  | Need approval | Need approval       |
| **Automatic**    | Performs safe actions without confirmation. | Run at once   | Need approval       |
| **Unrestricted** | Performs everything without confirmation.   | Run at once   | Run at once         |

The default mode is **Ask**. Read-only tools never need approval.

> [!WARNING]
> In **Unrestricted** mode, the agent can delete runs or submit paid runs with no approval. Use this mode only when you trust the task.

### Set the default mode

1. Open **Settings**.
2. Open the **Agent panel** tab.
3. Under **Agent trust level**, choose **Ask first**, **Automatic for safe actions only** or **No prior approval**.

### Approve an action

When an action needs approval, the panel shows an approval card. The card has a confirm button for the action, and **Cancel**. Click **Show technical details** to see the exact tool call.

If you do not answer within 15 minutes, the agent treats the action as declined.

## Attach a file

You can attach files to a message. Each file can be up to 20 MB. You can attach one dataset at a time.

To upload a dataset through the agent:

1. Attach a CSV, JSON or Excel file.
2. Check the upload card. It shows the columns of the file.
3. Set the column roles on the card, then confirm.

If the agent cannot read the file, the card shows "Couldn't read the file. Use a CSV, JSON, or XLSX file."

For file rules, see [Prepare a dataset](/docs/guides/datasets).

## Choose a model

The model menu in the composer has these groups:

- **Auto** lets Skynet choose the model.
- **Skynet credits** uses a hosted model, paid from your balance.
- **Bring your own key** uses a model through your own provider key.

Some models let you set the reasoning effort. The levels are **None**, **Minimal**, **Low**, **Medium**, **High**, **Extra high** and **Max**. **Default** uses the model's default level.

To set a default model for new chats, open **Settings** > **Agent panel** > **Default model**.

### Cost

Agent chats on Skynet credits use your balance. If your balance is empty, the panel shows "Your balance is empty. Add funds to keep chatting with the agent." For prices, read [Billing](/docs/concepts/billing).

## Tips

> [!TIP]
> Ask the agent to explain a run before you change it. For example: "Why did run 42 score lower than run 41?"

> [!TIP]
> Use **Ask** mode when you try a new kind of request. Change to **Automatic** when you know what the agent will do.

## Other ways to use an agent

You can also connect your own AI client to Skynet through the MCP server. See [MCP](/docs/mcp). For scripts, use the [API](/docs/api).

## Next steps

- [Submit a run](/docs/guides/submit).
- [Read results](/docs/guides/results).
- Read [Security](/docs/security) for how Skynet protects your keys.
