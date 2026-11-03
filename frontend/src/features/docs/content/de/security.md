Diese Seite beschreibt, wie Skynet Ihr Konto, Ihre Schlüssel und Ihre Daten schützt. Sie erklärt auch, wie Sie ein Sicherheitsproblem melden.

## Eine Schwachstelle melden

Melden Sie Sicherheitsprobleme vertraulich. Eröffnen Sie kein öffentliches Issue.

- Öffnen Sie auf GitHub das Skynet-Repository und gehen Sie zu **Security** > **Report a vulnerability**.
- Oder senden Sie eine E-Mail an `security@skynetml.com`.

Das Team bemüht sich, jede Meldung innerhalb von fünf Werktagen zu bestätigen.

## Ihr Konto

### Anmeldemethoden

Sie können sich anmelden mit:

- Einer E-Mail-Adresse und einem Passwort.
- **Mit Google fortfahren** oder **Mit GitHub fortfahren**.
- **Mit Passkey anmelden**. Ein Passkey verwendet Face ID, Touch ID oder einen Hardwareschlüssel.

Die Anbieter, die Sie verwenden, verwalten Sie unter **Verknüpfte Konten**.

### Passwortregeln

Ein Passwort muss:

- Mindestens 8 Zeichen haben.
- Höchstens 128 Zeichen haben.
- Ihre E-Mail-Adresse nicht enthalten.

Ein neues Konto muss seine E-Mail-Adresse mit einem Code bestätigen. Auch beim Zurücksetzen des Passworts sendet Skynet einen Code per E-Mail.

Nach wiederholten fehlgeschlagenen Versuchen sperrt Skynet die Anmeldung für eine E-Mail-Adresse für eine gewisse Zeit.

### Zwei-Faktor-Authentifizierung

Aktivieren Sie die Zwei-Faktor-Authentifizierung für mehr Schutz. Wählen Sie eine Methode:

| Methode               | So funktioniert sie                                                    |
| --------------------- | ---------------------------------------------------------------------- |
| **Authenticator-App** | Geben Sie einen 6-stelligen Code aus Ihrer Authenticator-App ein.      |
| **E-Mail-Codes**      | Geben Sie einen 6-stelligen Code ein, den Skynet bei jeder Anmeldung sendet. |

Skynet gibt Ihnen außerdem Wiederherstellungscodes. Bewahren Sie sie an einem sicheren Ort auf. Verwenden Sie einen davon, wenn Sie den Zugriff auf Ihre Methode verlieren.

> [!NOTE]
> Wenn Sie sich mit Google oder GitHub anmelden, verwaltet dieser Anbieter Ihre Zwei-Faktor-Authentifizierung.

## Wo Ihr Code läuft

Nutzer-Workloads laufen in isolierten Sandboxes. Im gehosteten Produkt ist Vercel Sandbox die Ausführungsgrenze. Das umfasst Konfigurationsprüfungen, Optimierungsdurchläufe, Wiederherstellung und Arbeit an abgeschlossenen Durchläufen.

Skynet-Worker führen Ihren Code nicht aus. Sie sind die Steuerungsebene. Sie starten Jobs, reservieren und verrechnen Budgets, geben Sandboxes eingeschränkte Anmeldedaten und speichern Checkpoints.

Bevor Skynet kostenpflichtige Arbeit startet, reserviert es Spielraum in Ihrem Wallet und im Budget des Durchlaufs. Siehe [Budgets und Abrechnung](/docs/concepts/billing).

In der [Mac-App](/docs/guides/mac-app) laufen Jobs in lokalen Docker-Containern. Alle lokalen Dienste lauschen nur auf `127.0.0.1`.

## Anbieterschlüssel (BYOK)

Sie können Ihre eigenen Schlüssel für Modellanbieter hinzufügen.

- Skynet verschlüsselt jeden Schlüssel im Ruhezustand.
- Skynet gibt den vollständigen Schlüssel nie über die API zurück.
- Skynet speichert nur die letzten vier Zeichen und einen Status: nicht geprüft, geprüft oder ungültig.
- Wenn Sie einen Schlüssel eingeben, sendet Skynet eine kleine Testanfrage, um ihn zu prüfen.

Ihr Anbieter rechnet Modell-Tokens über Ihren Schlüssel ab. Skynet erhebt keine Modellgebühr. Das Laufbudget deckt nur die Nutzung der Sandbox ab.

Skynet entfernt außerdem Geheimnisse aus Logs und Fehlermeldungen. Es ersetzt sie durch `[REDACTED]`.

## API-Tokens

Mit API-Tokens können Skripte und Werkzeuge die Skynet-API aufrufen.

- Jeder Nutzer hat höchstens ein gültiges Token. Wenn Sie ein neues Token generieren, funktioniert das alte Token nicht mehr.
- Tokens beginnen mit `skyd_`.
- Skynet speichert nur einen Hash des Tokens.
- Skynet zeigt das vollständige Token nur einmal. Kopieren und speichern Sie das Token sofort.
- Danach zeigt Skynet nur eine maskierte Form: `skyd_…` und die letzten vier Zeichen.

Senden Sie das Token im Header `Authorization`:

```http
Authorization: Bearer skyd_...
```

Um ein Token ungültig zu machen, klicken Sie auf **Token widerrufen**. Die vollständige API beschreibt [API](/docs/api).

> [!WARNING]
> Behandeln Sie das Token wie ein Passwort. Jeder mit dem Token kann über die API in Ihrem Namen handeln. Committen Sie es nicht in ein Repository.

## Datenschutz bei Modellen

Die Einstellung **Datenschutz bei Modellen** legt fest, was Modellanbieter mit Ihren Daten tun dürfen. Sie gilt für Optimierungen, die Skynet über Ihr Guthaben abrechnet.

| Option              | Wirkung                                                                                                   |
| ------------------- | --------------------------------------------------------------------------------------------------------- |
| **Alle erlauben**   | Jeder Anbieter, auch Anbieter, die mit Ihren Prompts trainieren oder sie speichern können.                |
| **Kein Training**   | Nur Anbieter, die nicht mit Ihren Prompts trainieren und sie nicht speichern. Das ist der Standard.        |
| **Keine Speicherung** | Nur Endpunkte mit einer Vereinbarung ohne Datenspeicherung (Zero Data Retention). Einige Modelle sind dann eventuell nicht verfügbar. |

> [!NOTE]
> BYOK-Aufrufe über OpenRouter folgen den Datenschutzeinstellungen Ihres OpenRouter-Kontos. BYOK-Aufrufe direkt bei einem Anbieter folgen dessen Datenrichtlinie.

## Sichtbarkeit von Durchläufen

> [!WARNING]
> Neue Durchläufe sind standardmäßig **Öffentlich**. Ein öffentlicher Durchlauf erscheint in der öffentlichen Suche. Jeder kann ihn finden und seine Ergebnisse durchsuchen.

Damit ein Durchlauf nicht in der öffentlichen Suche erscheint, setzen Sie beim Einreichen **Sichtbarkeit** auf **Privat**. Sie können die Sichtbarkeit später im Freigabedialog ändern. Siehe [Teilen und veröffentlichen](/docs/guides/sharing).

Neue Datensätze sind **Eingeschränkt**. Nur Sie und die Personen, die Sie einladen, können sie öffnen, bis Sie den Link-Zugriff ändern. Siehe [Einen Datensatz vorbereiten](/docs/guides/datasets).

## Ihre Daten

Öffnen Sie in **Einstellungen** den Tab **Datenschutz und Daten**, um Ihre Daten zu verwalten.

### Ihre Daten exportieren

Klicken Sie unter **Meine Daten exportieren** auf **Exportieren**. Der Export enthält alle Daten Ihres Kontos. Wählen Sie ein Format: JSON, CSV, Excel, Pickle, Parquet oder Feather.

### Ihr Konto löschen

Klicken Sie auf **Konto löschen**. Geben Sie Ihr aktuelles Passwort ein und tippen Sie zur Bestätigung Ihre E-Mail-Adresse ein.

> [!WARNING]
> Dadurch werden Ihr Konto und alle zugehörigen Daten dauerhaft gelöscht. Sie können das nicht rückgängig machen.

### Nutzungsanalyse

Die Einstellung **Anonyme Nutzungsanalyse senden** schaltet die anonyme Produktanalyse ein oder aus.

## Nächste Schritte

- Lesen Sie [Limits](/docs/limits).
- Lesen Sie die [FAQ](/docs/faq).
