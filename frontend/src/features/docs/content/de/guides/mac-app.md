Die Skynet-Mac-App führt Skynet auf Ihrem eigenen Computer aus. Sie sendet Modellaufrufe über eine Coding-CLI, die auf Ihrem Mac angemeldet ist. So bezahlt Ihr CLI-Tarif diese Aufrufe. Sie können die App auch mit Ihrem gehosteten Skynet-Konto verknüpfen.

## Voraussetzungen

- Ein Mac mit Apple-Chip. Es gibt Intel-Builds, sie wurden aber nicht auf Intel-Hardware getestet.
- Docker Desktop, gestartet, mit aktivierter Option **Use Rosetta for x86_64/amd64 emulation**.
- Eine angemeldete Coding-CLI:
  - Claude Code (`claude`) 2.1.226 oder neuer, oder
  - Codex (`codex`) 0.160.0 oder neuer.

> [!WARNING]
> Codex muss mit einem ChatGPT-Konto angemeldet sein. Eine Anmeldung mit einem API-Schlüssel zählt nicht. Eine Anmeldung, die im macOS-Schlüsselbund gespeichert ist, zählt ebenfalls nicht.

### Codex einrichten

Codex muss seine Anmeldedaten in einer Datei speichern. Führen Sie diese Schritte vor dem ersten Start aus:

1. Öffnen Sie `~/.codex/config.toml` und fügen Sie diese Zeile hinzu:

```toml
cli_auth_credentials_store = "file"
```

2. Melden Sie sich mit Ihrem ChatGPT-Konto an:

```bash
codex login
```

## Die App installieren

Die Builds sind ad hoc signiert und nicht notarisiert. macOS blockiert sie, bis Sie das Quarantäne-Attribut entfernen.

1. Öffnen Sie die `.dmg`-Datei.
2. Ziehen Sie **Skynet** in **Programme**.
3. Führen Sie diesen Befehl im Terminal aus:

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Öffnen Sie Skynet aus **Programme**.

> [!NOTE]
> Der erste Start dauert einige Minuten. Die App baut ein lokales Sandbox-Image und bereitet ihre Datenbank vor.

## Was beim Start passiert

Die App führt diese Schritte der Reihe nach aus:

1. Sie liest den `PATH` aus Ihrer Login-Shell, damit sie Docker und Ihre CLI findet.
2. Sie prüft Docker und Ihre Coding-CLI.
3. Sie startet eine lokale Postgres-Datenbank in Docker.
4. Sie baut das Sandbox-Image `skynet-local-sandbox` für `linux/amd64`. Docker führt es unter Rosetta aus.
5. Sie aktualisiert das Datenbankschema.
6. Sie startet die lokale API und die Web-App und zeigt die Web-App im Fenster.

### Einrichtungsprüfungen

Die App zeigt eine Liste von Prüfungen: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** und **Account**. Schlägt eine Prüfung fehl, zeigt die App einen Bildschirm, der Ihnen sagt, was zu tun ist:

| Bildschirm                 | Was zu tun ist                                                                    |
| -------------------------- | --------------------------------------------------------------------------------- |
| **Install Docker Desktop** | Installieren Sie Docker Desktop.                                                  |
| **Start Docker**           | Starten Sie Docker Desktop.                                                       |
| **Install a coding CLI**   | Installieren Sie Claude Code oder Codex.                                          |
| **Update your coding CLI** | Aktualisieren Sie die CLI auf die oben genannte Mindestversion.                   |
| **Sign in to your CLI**    | Melden Sie sich im Terminal bei der CLI an.                                       |
| **Usage limit reached**    | Warten Sie bis zum Zurücksetzen. Danach können wieder neue Jobs starten.          |

Klicken Sie auf **Check again**, nachdem Sie das Problem behoben haben. Klicken Sie auf **Open logs**, um die Protokolldateien zu sehen. Auf dem Bildschirm **Usage limit reached** können Sie auf **Open Skynet anyway** klicken.

## Lokale Durchläufe

In der App laufen Modellaufrufe über die CLI auf Ihrem Mac. Das Modellmenü zeigt diesen Modus als **Lokal**. Der Hinweis lautet: „Dein Tarif bezahlt die Aufrufe, daher fügt dieses Modell dem Lauf 0 $ hinzu.“

Die CLI-Namen in der App sind **Claude Code** und **Codex**.

> [!NOTE]
> Die App entfernt API-Schlüssel aus der Umgebung der Prozesse, die sie startet. So verwendet die CLI Ihr Abonnement und keinen API-Schlüssel.

### Den Proposer auf Ihrer CLI ausführen

Die Einstellung **Proposer auf meiner CLI ausführen** legt fest, wo der Proposer-Agent läuft. Die Optionen sind **Agent-Engine (Standard)** und **Aus (isolierter Proposer)**.

**Proposer-Zugriff** legt fest, worauf die CLI zugreifen darf:

- **Nur Arbeitsbereich** ist der Standard. Diese Option verlässt sich auf die eigene Sandbox der jeweiligen CLI.
- **Vollzugriff** führt die CLI mit Ihren Benutzerrechten aus.

> [!WARNING]
> Mit **Vollzugriff** kann die CLI jede Datei lesen, ändern und löschen, auf die Sie Zugriff haben, und auf das Netzwerk zugreifen. Der acceptEdits-Modus von Claude isoliert das Netzwerk nicht, auch nicht mit **Nur Arbeitsbereich**.

### Taggen in der App

Das Taggen mit KI benötigt in der Mac-App Ihren eigenen Anbieterschlüssel. Wählen Sie ein Modell unter **Dein eigener Schlüssel**. Siehe [Daten mit dem Tagger labeln](/docs/guides/tagger).

## Mit Ihrem Skynet-Konto verknüpfen

Sie können die App ohne Konto verwenden. Klicken Sie dazu auf **Use without signing in**. Um mit Ihrem gehosteten Konto zu synchronisieren, verknüpfen Sie die App.

### Diesen Mac verknüpfen

1. Im Schritt **Sign in to Skynet** öffnet die App in Ihrem Standardbrowser die Seite **Link a Mac**.
2. Melden Sie sich im Browser bei Skynet an, falls Sie dazu aufgefordert werden.
3. Prüfen Sie die Liste unter **Nach der Verknüpfung kann dieser Mac:**.
4. Klicken Sie auf **Diesen Mac verknüpfen**. Die Seite zeigt „Zurück zur Mac-App …“.

Schlägt die Anmeldung fehl oder brechen Sie ab, zeigt die App **Try again** und **Use without signing in**.

Nach der Verknüpfung kann der Mac:

- Ihren Namen und Ihre E-Mail-Adresse sehen.
- Ihr Guthaben sehen.
- Ihre Durchläufe mit Ihrem Konto synchronisieren.
- Gehostete Modelle nutzen, bezahlt aus Ihrem Guthaben.
- Ihre verbundenen Datenquellen nutzen.
- Über Skynet im Web suchen und surfen.

Die Verknüpfung verwendet einen sicheren Anmeldeablauf (OAuth mit PKCE). Die App speichert ihre Tokens verschlüsselt auf Ihrem Mac. Wenn Sie die Verknüpfung aufheben, widerruft Skynet die Tokens.

### Limits der Verknüpfung

| Limit                              | Wert                                        |
| ---------------------------------- | ------------------------------------------- |
| Verknüpfte Macs pro Konto          | 10                                          |
| Gültigkeitsdauer des Zugriffstokens | 1 Stunde, danach erneuert die App es       |
| Inaktivitätszeitlimit der Verknüpfung | 30 Tage ohne Nutzung                     |
| Maximales Alter der Verknüpfung    | 365 Tage                                    |

Nach dem Inaktivitätszeitlimit oder dem maximalen Alter verknüpfen Sie den Mac erneut.

### Status der Verknüpfung

Die App zeigt einen dieser Status: **Lokaler Modus**, **Mit diesem Mac verknüpft**, **Zum Synchronisieren anmelden** oder **Verknüpft (offline)**.

## Lokale Dienste und Daten

Alle lokalen Dienste lauschen nur auf `127.0.0.1`:

| Dienst   | Portbereich    |
| -------- | -------------- |
| API      | 48100 bis 48119 |
| Web-App  | 48120 bis 48139 |
| Postgres | 48140 bis 48159 |

Die App speichert ihre Daten in `~/Library/Application Support/Skynet`. Der Ordner `logs/` enthält `desktop.log`, `api.log`, `frontend.log`, `migrate.log` und `sandbox-build.log`.

Wenn Sie die App beenden, stoppt sie die Web-App, die API, Postgres und alle Job-Container.

### Die App zurücksetzen

> [!WARNING]
> Diese Befehle löschen alle lokalen Daten, einschließlich Ihrer lokalen Durchläufe und Datensätze.

Beenden Sie zuerst die App. Führen Sie dann aus:

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Nächste Schritte

- [Starten Sie einen Durchlauf](/docs/guides/submit).
- Lesen Sie mehr über die [Abrechnung](/docs/concepts/billing) gehosteter Modelle.
- Lesen Sie [Sicherheit](/docs/security).
