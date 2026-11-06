Mit dem Skynet-MCP-Server kann ein KI-Agent Skynet für Sie verwenden. Claude, ChatGPT, Codex, Cursor und andere MCP-Clients können eine Durchlaufkonfiguration prüfen, Durchläufe starten, beobachten und stoppen. Sie genehmigen die Verbindung einmal in Ihrem Browser. Der Agent legt für jeden Durchlauf ein Ausgabenlimit fest.

## Server-URL

Alle Clients verwenden dieselbe URL:

```text
https://mcp.skynetml.com/mcp
```

Der Server verwendet Streamable HTTP. Die Anmeldung erfolgt über OAuth 2.1. Sie kopieren weder ein Token noch einen API-Schlüssel in den Client.

## Ihren Client verbinden

Wählen Sie Ihren Client aus. Folgen Sie seinen Schritten und gehen Sie dann zu [Die Verbindung genehmigen](#die-verbindung-genehmigen).

```skynet-mcp-clients

```

## Die Verbindung genehmigen

Wenn der Agent Skynet zum ersten Mal aufruft, öffnet Ihr Browser eine Skynet-Seite.

1. Melden Sie sich bei Skynet an, falls Sie noch nicht angemeldet sind.
2. Lesen Sie die Seite. Sie zeigt den Namen des Agenten, die Website, zu der er zurückkehrt, und Ihr Konto.
3. Lesen Sie, was der Agent tun kann:
   - Ihre Optimierungsdurchläufe und deren Ergebnisse sehen.
   - Optimierungsdurchläufe starten und stoppen, bezahlt aus Ihrem Guthaben.
4. Klicken Sie auf **Allow**, um die Verbindung herzustellen. Klicken Sie auf **Deny**, um abzubrechen.

Nachdem Sie auf **Allow** geklickt haben, kehrt der Browser zum Agenten zurück. Der Agent kann jetzt die unten aufgeführten Werkzeuge aufrufen.

> [!NOTE]
> Der Agent legt für jeden Durchlauf ein Ausgabenlimit fest. Er kann kein Guthaben aufladen.

## Werkzeuge

| Werkzeug    | Was es tut                                                                                          | Zugriff   |
| ----------- | --------------------------------------------------------------------------------------------------- | --------- |
| `check_run` | Prüft eine Durchlaufkonfiguration, ohne sie auszuführen oder Ihnen etwas zu berechnen. Schlägt ein Ausgabenlimit vor. | Lesen     |
| `start_run` | Startet einen Durchlauf mit einem Ausgabenlimit.                                                    | Schreiben |
| `get_run`   | Gibt den Status, die Ausgaben und nach dem Ende des Durchlaufs das Ergebnis zurück.                 | Lesen     |
| `list_runs` | Listet Ihre Durchläufe auf, die neuesten zuerst. Kann nach Status filtern.                          | Lesen     |
| `stop_run`  | Stoppt einen Durchlauf. Ein zweiter Aufruf gibt einen Fehler zurück, der besagt, dass der Durchlauf bereits beendet ist. | Schreiben |

Lesezugriff verwendet den Scope `runs:read`. Schreibzugriff verwendet den Scope `runs:write`.

Kein Werkzeug lädt Guthaben auf, ändert Ihr Guthaben oder ändert Abrechnungseinstellungen.

## So führt ein Agent eine Optimierung aus

1. Der Agent ruft `check_run` mit der Durchlaufkonfiguration auf. Skynet führt die statischen Prüfungen aus und gibt zurück:
   - Das Ergebnis jeder Prüfung.
   - Das niedrigste Ausgabenlimit, mit dem der Durchlauf starten kann.
   - Ein vorgeschlagenes Ausgabenlimit. Es beträgt mindestens 5,00 $.
   - Die Modelle, für die kein Preis bekannt ist.
   - Den Betrag, den Sie jetzt ausgeben können.
2. Der Agent ruft `start_run` mit einem Ausgabenlimit auf (`max_cost_cents`, von 1 bis 1.000.000 US-Cent). Er kann auch einen `idempotency_key` senden, damit ein erneuter Versuch keinen zweiten Durchlauf startet.
3. Läuft die Konfigurationsprüfung noch, gibt `start_run` `in_progress` und `retry_after_seconds` zurück. Nach dieser Zeit ruft der Agent `start_run` erneut mit derselben Eingabe auf.
4. Der Agent ruft `get_run` auf, bis der Durchlauf endet. Die Endstatus sind `success`, `failed`, `cancelled` und `stopped`.

> [!TIP]
> Nennen Sie dem Agenten Ihr Ausgabenlimit im Prompt, zum Beispiel „Optimiere dieses Repository. Gib höchstens 10 $ aus.“ Der Agent verwendet dieses Limit dann in `start_run`.

Der Durchlauf stoppt, wenn er sein Ausgabenlimit erreicht. Wie Skynet einen Durchlauf abrechnet, lesen Sie unter [Abrechnung](/docs/concepts/billing).

## Sicherheit

- Jeder Client erhält seine eigene Berechtigung. Der Client sieht Ihr Passwort nie.
- Die Anmeldung verwendet PKCE (S256). Der Client registriert sich selbst über die dynamische Client-Registrierung oder ein Client-ID-Metadatendokument.
- Zugriffstokens laufen nach 1 Stunde ab. Der Client erneuert sie mit einem Refresh-Token.
- Ein Refresh-Token läuft nach 30 Tagen ohne Nutzung ab. Eine Berechtigung gilt höchstens 90 Tage. Danach genehmigen Sie die Verbindung erneut.
- Jedes Refresh-Token funktioniert nur einmal. Verwendet jemand ein altes Refresh-Token erneut, beendet Skynet die Berechtigung.
- Der Agent kann nur Ihre eigenen Durchläufe sehen und ändern.

Wie Skynet Ihr Konto und Ihre Daten schützt, lesen Sie unter [Sicherheit](/docs/security).

## Verbindung trennen

Um die Verbindung eines Agenten zu trennen, entfernen Sie den Skynet-Server aus dem Client. Die meisten Clients widerrufen dann ihre Tokens. Widerruft ein Client sie nicht, läuft sein Refresh-Token nach 30 Tagen ohne Nutzung ab.

## Fehlerbehebung

| Problem                                              | Was zu tun ist                                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Die Verbindung schlägt sofort fehl                   | Prüfen Sie die Server-URL. Sie muss genau `https://mcp.skynetml.com/mcp` lauten.     |
| "This agent can't connect"                           | Starten Sie die Verbindung im Client erneut.                                         |
| "Too many attempts"                                  | Warten Sie einige Minuten und versuchen Sie es dann erneut.                          |
| Der Client öffnet keinen Browser                     | Aktualisieren Sie den Client. Er muss Remote-MCP-Server mit OAuth unterstützen.      |
| `start_run` schlägt mit einem Guthabenfehler fehl    | Laden Sie in Skynet Guthaben auf oder starten Sie den Durchlauf mit einem niedrigeren Ausgabenlimit. |
| Die Werkzeuge funktionieren nach einigen Wochen nicht mehr | Die Berechtigung ist abgelaufen. Entfernen Sie den Server und verbinden Sie ihn erneut. |

Für direkten HTTP-Zugriff ohne Agent lesen Sie [API](/docs/api).
