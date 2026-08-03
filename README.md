# DDBdownloader

Ein robustes Python-Skript zum massenhaften Herunterladen von Objektdaten aus der API der Deutschen Digitalen Bibliothek (DDB).

## Features

- Liest IDs über die Solr-Suche mit `cursorMark` (Default: `rows=100000`). Cursor-Paginierung vermeidet bei großen Treffermengen die wachsenden Kosten und Grenzen von `start`-Offsets.
- Lädt pro ID parallel (Default: 16 Threads) einen auswählbaren API-Endpunkt.
- Unterstützte Ziele:
  - `edm-europeana` → `/items/{id}/edm` mit Europeana-EDM-Profil
  - `edm-ddb` → `/items/{id}/edm` ohne Profilheader
  - `aip` → `/items/{id}`
  - `binaries` → `/items/{id}/binaries`
  - `iiif` → `/items/{id}/iiif`
  - `source` → `/items/{id}/source/record`
  - `view` → `/items/{id}/view`
- Setzt `accept-profile: https://www.deutsche-digitale-bibliothek.de/ns/europeana-edm-profile` ausschließlich für `edm-europeana`.
- Schreibt pro Objekt eine zielabhängige Datei `{id}.xml`, `{id}.json`, `{id}.bin` oder `{id}.html` ins ZIP.
- Optionales ZIP-Splitting über `-b/--batch` (z.B. 1000 Dateien pro ZIP → `output-1.zip`, `output-2.zip`, ...).
- Fortsetzen ist standardmäßig aktiv: Lesbare bestehende ZIP-Einträge werden übersprungen. Beschädigte ZIP-Dateien werden nicht fortgesetzt und als `.corrupt-<Zeitstempel>` ausgesondert.
- Statusanzeige während Laufzeit mit Fortschritt, Rate, ETA, übertragener Größe, Datenrate und erwarteter Gesamtgröße.
- Logdatei mit Rotation (Default: `output.log`).
- Abschluss-Statistik als JSON.

## Voraussetzungen

- Windows mit installiertem Python (empfohlen: über `py`-Launcher).
- Python-Paket `requests`:

```powershell
py -m pip install requests
```

## API-Key

Für den EDM-Download über die API v2 wird _kein_ API-Key benötigt.

## Aufruf

Direkt über Python:

```powershell
py .\DDBdownloader.py -q "dataset_id:73873569924928456gWuT" -o "output.zip" --target iiif -b 1000
```

## Kleine GUI

Es gibt eine minimalistische GUI auf Basis von Tkinter, die den bestehenden Downloader unverändert als Subprozess startet und dessen Statusausgaben anzeigt.

Start:

```powershell
py .\DDBdownloader_gui.py
```

In der GUI:
- API, Ziel, Query, Output und Batch setzen
- ZIP-Fortsetzen aktiv lassen, um einen abgebrochenen Lauf sicher weiterzuführen
- `Start` startet den Download
- `Stop` beendet den laufenden Prozess
- Live-Status erscheint oben; Details laufen in der Textbox und in `output.log`
 
Hinweis: Die Thread-Anzahl ist in der GUI absichtlich nicht konfigurierbar; der Downloader nutzt seinen Default (16).

## Windows-Release (EXE)

Dieses Repo enthält eine GitHub Action, die bei einem Tag (z.B. `v1.0.0`) automatisch ein ZIP für Windows baut und als Release-Asset hochlädt.

Ablauf für Nutzer:
- In GitHub → **Releases** das ZIP herunterladen
- ZIP entpacken
- `DDBdownloader_gui.exe` starten (Doppelklick)

Hinweis: Im ZIP liegt zusätzlich `DDBdownloader.exe` (CLI). Die GUI nutzt diese Datei intern zum Starten des Downloads.

## Parameter

- `-q/--query` – Solr-Query (wird als `q=` an die Solr-Select-API übergeben)
- `-o/--output` – Name der ZIP-Ausgabe (Logdatei wird daneben als `output.log` erstellt)
- `--target` – abzurufender Endpunkt: `edm-europeana`, `edm-ddb`, `aip`, `binaries`, `iiif`, `source` oder `view`
- `--no-resume` – vorhandene Ausgabe nicht fortsetzen, sondern überschreiben
- `-b/--batch` – maximale Anzahl Dateien pro ZIP (0 = alles in eine ZIP)
- `--api` – API-Basis (z.B. `https://api.deutsche-digitale-bibliothek.de` oder `https://api-q1.deutsche-digitale-bibliothek.de`)
- `--threads` – Download-Parallelität (Default: 16)
- `--rows` – IDs pro Solr-Seite (Default: 100000)
- `--pagination` – Solr-Paginierung: `cursor` (Standard, CursorMark) oder `start` (Offset-Paginierung)
- `--timeout` – HTTP Timeout für Item-Downloads (Default: 30s)
- `--solr-timeout` – Read-Timeout für Solr-ID-Abfragen (Default: 180s)
- `--retries` – Retries pro Objekt (Default: 4, zusätzlich zum ersten Versuch)
- `--backoff` – Basis für exponentiellen Backoff (Default: 1.0)

## Ausgaben

- ZIP-Datei(en), standardmäßig beim nächsten Lauf fortsetzbar:
  - ohne `-b`: genau `output.zip`
  - mit `-b`: `output-1.zip`, `output-2.zip`, ...
- Log: `output.log` (rotierend, damit es nicht unendlich groß wird)
- Konsole:
  - zunächst: Anzahl gefundener IDs
  - während des Downloads: Statuszeile (Fortschritt, Fehlerzähler, Rate, ETA, Größe und Größenprognose)
  - am Ende: Statistik als JSON

## Hinweise für sehr große Datenmengen

- Das Skript lädt die IDs nicht komplett in RAM, sondern streamt sie über eine temporäre Datei.
- Die Anzahl gleichzeitig „ausstehender“ Download-Aufgaben ist begrenzt, um Memory-Spikes bei Millionen IDs zu vermeiden.
- Bei Solr- und API-Problemen (Timeouts, 429, 5xx, ungültige Solr-Antworten, leere Antworten) werden Retries versucht und alles in der Logdatei protokolliert.
