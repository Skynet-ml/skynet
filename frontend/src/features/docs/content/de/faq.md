Diese Seite beantwortet häufige Fragen zu Skynet. Jede Antwort verweist auf die Seite mit allen Details.

## Allgemein

### Was ist Skynet?

Skynet ist eine Plattform, die DSPy-Programme und Repositorys anhand Ihrer eigenen Daten und Metrik optimiert. Sie laden einen Datensatz hoch, beschreiben die Aufgabe und starten einen Durchlauf. Der Durchlauf probiert neue Versionen aus und bewertet sie auf zurückgehaltenen Daten. Lesen Sie zum Einstieg den [Schnellstart](/docs/quickstart).

### Was kann Skynet optimieren?

Skynet ist Optimize Anything. Es optimiert DSPy-Programme und Repositorys in einem Produkt. Die Artefaktart entscheidet, welche Engines damit arbeiten können:

- Repositorys nutzen die nativen Engines: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler und ShinkaEvolve.
- DSPy-Programme verwenden die DSPy-Optimierer: DSPy GEPA, DSPy MIPROv2 und DSPy BootstrapFewShot.

Bei einem DSPy-Programm verwendet **Auto** DSPy GEPA. Bei einem Repository wählen Sie eine native Engine. Lesen Sie [So funktioniert die Optimierung](/docs/concepts/optimization) und [Engines](/docs/concepts/engines).

### Welche Sprachen unterstützt die Oberfläche?

Die Oberfläche gibt es in 24 Sprachen. Hebräisch ist die Basissprache, und das Layout unterstützt Text von rechts nach links. Die hebräische Dokumentation finden Sie unter [/docs/he](/docs/he).

### Ist Skynet Open Source?

Ja. Der Code steht unter der Lizenz AGPL-3.0. Wenn Sie eine geänderte Version von Skynet als Dienst anbieten, verlangt die Lizenz, dass Sie Ihre Änderungen veröffentlichen.

## Daten

### Welche Dateitypen kann ich hochladen?

Sie können CSV-, JSON-, `.xlsx`- und `.xls`-Dateien hochladen. Bei Excel-Dateien liest Skynet nur das erste Tabellenblatt. Siehe [Einen Datensatz vorbereiten](/docs/guides/datasets).

### Meine Daten haben keine Labels. Was kann ich tun?

Verwenden Sie den Tagger. Sie können Zeilen selbst labeln oder ein KI-Modell labeln lassen, während Sie seine Arbeit prüfen. Siehe [Daten mit dem Tagger labeln](/docs/guides/tagger).

### Wer kann meine Durchläufe und Datensätze sehen?

> [!WARNING]
> Neue Durchläufe sind standardmäßig **Öffentlich**. Ein öffentlicher Durchlauf erscheint in der öffentlichen Suche, und jeder kann seine Ergebnisse durchsuchen.

Setzen Sie beim Einreichen eines Durchlaufs **Sichtbarkeit** auf **Privat**, damit er nicht in der öffentlichen Suche erscheint. Neue Datensätze sind **Eingeschränkt**: Nur Sie und die Personen, die Sie einladen, können sie öffnen. Siehe [Teilen und veröffentlichen](/docs/guides/sharing) und [Sicherheit](/docs/security).

### Trainieren Modellanbieter mit meinen Prompts?

Standardmäßig nicht. Die Standardoption für **Datenschutz bei Modellen** ist **Kein Training**. Sie sendet Prompts nur an Anbieter, die nicht damit trainieren und sie nicht speichern. Diese Einstellung gilt für Aufrufe, die Skynet über Ihr Guthaben abrechnet. BYOK-Aufrufe über OpenRouter folgen den Datenschutzeinstellungen Ihres OpenRouter-Kontos. BYOK-Aufrufe direkt bei einem Anbieter folgen dessen Datenrichtlinie. Siehe [Sicherheit](/docs/security).

## Abrechnung

### Wie rechnet Skynet Durchläufe ab?

Skynet verwendet ein Prepaid-Guthaben. Die Nutzung verwalteter Modelle und deren Aufschlag sowie die Sandbox-Nutzung werden gemessen. Beim Aufladen fällt eine Gebühr an. Bevor kostenpflichtige Arbeit beginnt, reserviert Skynet Spielraum in Ihrem Wallet und im Budget des Durchlaufs. Siehe [Budgets und Abrechnung](/docs/concepts/billing).

### Kann ich meinen eigenen Schlüssel eines Modellanbieters verwenden?

Ja. Bei Nutzung eines eigenen Schlüssels (BYOK) gilt: Ihr Anbieter rechnet Modell-Tokens über Ihren Schlüssel ab. Skynet erhebt keine Modellgebühr. Das Laufbudget deckt nur die Nutzung der Sandbox ab. Skynet verschlüsselt Ihren Schlüssel und zeigt ihn nie wieder an. Siehe [Budgets und Abrechnung](/docs/concepts/billing) und [Sicherheit](/docs/security).

### Kann ich Skynet nutzen, ohne für Modellaufrufe zu bezahlen?

Die [Mac-App](/docs/guides/mac-app) führt Skynet auf Ihrem eigenen Computer aus. Sie sendet Modellaufrufe über Claude Code oder Codex, angemeldet auf Ihrem Mac. Ihr CLI-Tarif bezahlt diese Aufrufe, daher fügen sie dem Durchlauf 0 $ hinzu.

## Werkzeuge und Integrationen

### Kann ich Skynet aus meinem eigenen KI-Client steuern?

Ja. Skynet hat einen MCP-Server unter `https://mcp.skynetml.com/mcp`. Sie melden sich mit OAuth an. Der Server hat diese Werkzeuge: `check_run`, `start_run`, `get_run`, `list_runs` und `stop_run`. Siehe [MCP-Server](/docs/mcp).

### Gibt es eine API?

Ja. Generieren Sie unter Einstellungen ein API-Token und senden Sie es dann als `Authorization: Bearer <token>`. Jeder Nutzer hat jeweils ein gültiges Token. Siehe [API](/docs/api).

### Kann der Agentenbereich meine Durchläufe löschen?

Nur, wenn Sie es erlauben. Im Standardmodus **Fragen** fragt der Agent vor jeder Aktion nach. Im Modus **Automatisch** fragt er weiterhin vor destruktiven Aktionen, etwa Löschen oder Einreichen. Siehe [Agentenbereich](/docs/guides/agent-panel).
