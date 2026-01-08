# DDBdownloader

Ein robustes Python-Skript zum massenhaften Herunterladen von EDM-XML-Daten aus der API der Deutschen Digitalen Bibliothek (DDB).

## Features

- Liest IDs über die Solr-Suche seitenweise (Default: `rows=100000`).
- Lädt EDM-XML pro ID parallel (Default: 16 Threads).
- Setzt die gewünschten Header:
  - `Accept: application/xml`
  - `accept-profile: https://www.deutsche-digitale-bibliothek.de/ns/europeana-edm-profile`
- Schreibt pro Objekt eine Datei `{id}.xml` ins ZIP.
- Optionales ZIP-Splitting über `-b/--batch` (z.B. 1000 Dateien pro ZIP → `output-1.zip`, `output-2.zip`, ...).
- Statusanzeige während Laufzeit (Fortschritt, Rate, ETA).
- Logdatei mit Rotation (Default: `output.log`).
- Abschluss-Statistik als JSON.

## Voraussetzungen

- Windows mit installiertem Python (empfohlen: über `py`-Launcher).
- Python-Paket `requests`:

```powershell
py -m pip install requests
```

## API-Key (.env)

Für den EDM-Download wird ein API-Key benötigt. Lege im Projektordner eine Datei `.env` an:

```text
DDB_API_KEY=DEIN_KEY
```

Vorlage: `.env.example` (diese Datei kann kopiert werden).

Der Key wird **nicht** als Header gesendet, sondern als Query-Parameter `oauth_consumer_key` an den Item-Endpunkt angehängt.
Im Log wird der Key nur gekürzt ausgegeben.

## Aufruf

Es gibt zwei Möglichkeiten:

1. Direkt über Python:

```powershell
py .\DDBdownloader.py -q "dataset_id:73873569924928456gWuT" -o "output.zip" -b 1000
```

2. Über den Windows-Wrapper (wie in der Aufgabenstellung):

```powershell
.\DDBdownloader.cmd -q "dataset_id:73873569924928456gWuT" -o "output.zip" -b 1000
```

## Kleine GUI

Es gibt eine minimalistische GUI auf Basis von Tkinter, die den bestehenden Downloader unverändert als Subprozess startet und dessen Statusausgaben anzeigt.

Start:

```powershell
py .\DDBdownloader_gui.py
```

In der GUI:
- Query, Output, Batch und Threads setzen
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
- `-b/--batch` – maximale Anzahl XML-Dateien pro ZIP (0 = alles in eine ZIP)
- `--threads` – Download-Parallelität (Default: 16)
- `--rows` – IDs pro Solr-Seite (Default: 100000)
- `--timeout` – HTTP Timeout (Default: 30s)
- `--retries` – Retries pro Objekt (Default: 4, zusätzlich zum ersten Versuch)
- `--backoff` – Basis für exponentiellen Backoff (Default: 1.0)

## Ausgaben

- ZIP-Datei(en):
  - ohne `-b`: genau `output.zip`
  - mit `-b`: `output-1.zip`, `output-2.zip`, ...
- Log: `output.log` (rotierend, damit es nicht unendlich groß wird)
- Konsole:
  - zunächst: Anzahl gefundener IDs
  - während des Downloads: Statuszeile (Fortschritt, Fehlerzähler, Rate, ETA)
  - am Ende: Statistik als JSON

## Hinweise für sehr große Datenmengen

- Das Skript lädt die IDs nicht komplett in RAM, sondern streamt sie über eine temporäre Datei.
- Die Anzahl gleichzeitig „ausstehender“ Download-Aufgaben ist begrenzt, um Memory-Spikes bei Millionen IDs zu vermeiden.
- Bei API-Problemen (Timeouts, 429, 5xx, leere Antworten) werden Retries versucht und alles in der Logdatei protokolliert.
