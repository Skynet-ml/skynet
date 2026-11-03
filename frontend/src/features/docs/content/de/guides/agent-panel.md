Der Agentenbereich ist ein Chat-Assistent in Skynet. Er kann Ihre Durchläufe, Datensätze und Ihr Guthaben lesen und für Sie handeln, etwa einen Durchlauf umbenennen oder abbrechen. Sie legen fest, welche Aktionen Ihre Freigabe benötigen.

## Den Bereich öffnen

Der Bereich öffnet sich am Rand der App. Wenn Sie ihn minimieren, bleibt eine Schaltfläche mit der Beschriftung **Agent öffnen** auf dem Bildschirm. Klicken Sie darauf, um den Bereich wieder zu öffnen.

Klicken Sie auf **Was ich kann**, um eine Liste der Werkzeuge des Agenten zu sehen.

### Konversationen

- **Neue Konversation** startet einen leeren Chat.
- **Frühere Konversationen** listet Ihre älteren Chats auf. Sie können sie durchsuchen, anheften, umbenennen und löschen.

### Eine Nachricht senden

Geben Sie Ihre Nachricht ein und drücken Sie die Eingabetaste. Sie können auch auf **Diktieren** klicken und Ihre Nachricht sprechen.

Während der Agent arbeitet, bleibt das Eingabefeld verfügbar:

- **Enter** steuert die laufende Arbeit, sofern dies unterstützt wird. Der Agent liest die Nachricht beim nächsten Schritt. **Tab** stellt eine neue Runde in die Warteschlange. Ohne Steuerungsfunktion reiht auch Enter die Nachricht ein.
- Unter **Ausstehende Nachrichten** können Sie Nachrichten bearbeiten, löschen und durch Ziehen neu anordnen. **Steuern** übergibt eine wartende Nachricht an die laufende Arbeit, sofern verfügbar.
- **Stoppen** beendet die laufende Runde und pausiert die Warteschlange. Ungelesene Steuerungsnachrichten kehren zurück. Nichts startet automatisch. **Esc** im Eingabefeld bewirkt dasselbe, außer bei geöffnetem Menü oder während einer IME-Texteingabe.
- Im Ruhezustand sendet **Jetzt senden** die erste Nachricht der pausierten Warteschlange. Die übrigen folgen der Reihe nach. Auch ein normaler Rundenabschluss startet die nächste Nachricht.
- **Stoppen und jetzt senden** stoppt die Runde und sendet den gesamten Wartebereich als eine neue Runde. Ungelesene Steuerungsnachrichten werden zuerst zurückgeholt und vor den übrigen Nachrichten mit Leerzeilen verbunden. Bereits gelesene Nachrichten werden nicht erneut gesendet. Bei unbestätigtem Rückruf bleibt die Warteschlange pausiert.
- Der Dokumentationsassistent und geschützte Chats mit bereitgestellten Programmen unterstützen **nur Warteschlangen**. Enter und Tab reihen dort Nachrichten ein. Nach einem Anfragelimit pausiert der Dokumentationsassistent die übrigen Nachrichten.

## Was der Agent kann

Der Agent kann lesen:

- Ihre Durchläufe, ihre Logs und ihre Testergebnisse.
- Analysen und Ihr Wallet-Guthaben.
- Die Datensätze in Ihrer Bibliothek und öffentliche Suchergebnisse.
- Ihre Tagging-Sitzungen (nur lesend).
- Die Beispieldatensätze, den Engine-Katalog und den Optimierer-Katalog.

Der Agent kann auch handeln. Zu den sicheren Aktionen gehören:

- Durchläufe umbenennen und anheften, auch viele Durchläufe auf einmal anheften.
- Einen Durchlauf pausieren.
- Den Code eines Durchlaufs bearbeiten und validieren.
- Einen Datensatz analysieren und seine Spaltenrollen festlegen.
- Verfügbare Modelle finden.
- Den Einreichungsassistenten und Ihre Einstellungen aktualisieren.
- Einen Beispieldatensatz bereitstellen.
- Einen Ordner erstellen.

Zu den destruktiven Aktionen gehören:

- Einen oder viele Durchläufe löschen.
- Einen Durchlauf einreichen, abbrechen, klonen, erneut versuchen, fortsetzen oder neu starten, und viele Durchläufe abbrechen.
- Einen Ordner löschen, verschieben oder aktualisieren und Durchläufe zwischen Ordnern verschieben.

> [!NOTE]
> Web-Werkzeuge (Suche, eine URL abrufen, eine Website crawlen) sind nur verfügbar, wenn auf dem Server ein Schlüssel für die Websuche eingerichtet ist. Sehen Sie sie nicht, bietet Ihre Skynet-Instanz sie nicht an.

## Vertrauensmodi

Der Vertrauensmodus legt fest, welche Aktionen Ihre Freigabe benötigen. Klicken Sie im Eingabebereich auf den Vertrauensschalter, um ihn zu ändern. Der Schalter wechselt der Reihe nach durch die drei Modi.

| Modus                | Beschreibung                                            | Sichere Aktionen      | Destruktive Aktionen  |
| -------------------- | ------------------------------------------------------- | --------------------- | --------------------- |
| **Fragen**           | Fragt vor jeder Aktion nach einer Bestätigung.          | Benötigen Freigabe    | Benötigen Freigabe    |
| **Automatisch**      | Führt sichere Aktionen ohne Bestätigung aus.            | Laufen sofort         | Benötigen Freigabe    |
| **Uneingeschränkt**  | Führt alles ohne Bestätigung aus.                       | Laufen sofort         | Laufen sofort         |

Der Standardmodus ist **Fragen**. Werkzeuge, die nur lesen, benötigen nie eine Freigabe.

> [!WARNING]
> Im Modus **Uneingeschränkt** kann der Agent ohne Freigabe Durchläufe löschen oder kostenpflichtige Durchläufe einreichen. Verwenden Sie diesen Modus nur, wenn Sie der Aufgabe vertrauen.

### Den Standardmodus festlegen

1. Öffnen Sie **Einstellungen**.
2. Öffnen Sie den Tab **Agentenbereich**.
3. Wählen Sie unter **Vertrauensstufe des Agenten** **Zuerst fragen**, **Automatisch nur für sichere Aktionen** oder **Keine vorherige Freigabe**.

### Eine Aktion freigeben

Benötigt eine Aktion eine Freigabe, zeigt der Bereich eine Freigabekarte. Die Karte hat eine Schaltfläche zum Bestätigen der Aktion und **Abbrechen**. Klicken Sie auf **Technische Details anzeigen**, um den genauen Werkzeugaufruf zu sehen.

Antworten Sie nicht innerhalb von 15 Minuten, behandelt der Agent die Aktion als abgelehnt.

## Eine Datei anhängen

Sie können Dateien an eine Nachricht anhängen. Jede Datei darf bis zu 20 MB groß sein. Sie können jeweils einen Datensatz anhängen.

So laden Sie einen Datensatz über den Agenten hoch:

1. Hängen Sie eine CSV-, JSON- oder Excel-Datei an.
2. Prüfen Sie die Upload-Karte. Sie zeigt die Spalten der Datei.
3. Legen Sie auf der Karte die Spaltenrollen fest und bestätigen Sie.

Kann der Agent die Datei nicht lesen, zeigt die Karte „Datei konnte nicht gelesen werden. Verwende eine CSV-, JSON- oder XLSX-Datei.“

Die Regeln für Dateien finden Sie unter [Einen Datensatz vorbereiten](/docs/guides/datasets).

## Ein Modell wählen

Das Modellmenü im Eingabebereich hat diese Gruppen:

- **Auto** lässt Skynet das Modell wählen.
- **Skynet-Credits** verwendet ein gehostetes Modell, bezahlt aus Ihrem Guthaben.
- **Eigener Schlüssel** verwendet ein Modell über Ihren eigenen Anbieterschlüssel.

Bei einigen Modellen können Sie den Denkaufwand festlegen. Die Stufen sind **Ohne**, **Minimal**, **Niedrig**, **Mittel**, **Hoch**, **Besonders hoch** und **Max**. **Standard** verwendet die Standardstufe des Modells.

Um ein Standardmodell für neue Chats festzulegen, öffnen Sie **Einstellungen** > **Agentenbereich** > **Standardmodell**.

### Kosten

Agenten-Chats mit Skynet-Credits belasten Ihr Guthaben. Ist Ihr Guthaben leer, zeigt der Bereich „Ihr Guthaben ist aufgebraucht. Laden Sie Guthaben auf, um weiter mit dem Agenten zu chatten.“ Die Preise finden Sie unter [Abrechnung](/docs/concepts/billing).

## Tipps

> [!TIP]
> Bitten Sie den Agenten, einen Durchlauf zu erklären, bevor Sie ihn ändern. Zum Beispiel: „Warum hat Durchlauf 42 schlechter abgeschnitten als Durchlauf 41?“

> [!TIP]
> Verwenden Sie den Modus **Fragen**, wenn Sie eine neue Art von Anfrage ausprobieren. Wechseln Sie zu **Automatisch**, wenn Sie wissen, was der Agent tun wird.

## Andere Wege, einen Agenten zu nutzen

Sie können auch Ihren eigenen KI-Client über den MCP-Server mit Skynet verbinden. Siehe [MCP](/docs/mcp). Für Skripte verwenden Sie die [API](/docs/api).

## Nächste Schritte

- [Starten Sie einen Durchlauf](/docs/guides/submit).
- [Lesen Sie die Ergebnisse](/docs/guides/results).
- Lesen Sie unter [Sicherheit](/docs/security), wie Skynet Ihre Schlüssel schützt.
