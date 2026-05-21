"""DDBdownloader

Massendownload von EDM-XML über die API der Deutschen Digitalen Bibliothek.

Ablauf:
- Solr-Suche (IDs seitenweise) -> IDs zählen
- EDM je ID parallel laden (robust mit Retries/Timeouts)
- als {id}.xml in ZIP-Archiv(e) schreiben
"""

import argparse
import json
import logging
import os
import queue
import signal
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Dict, Iterable, Optional, Tuple


# Kooperatives Stop-Signal: wird von Signal-Handlern und dem GUI gesetzt.
_stop_event = threading.Event()


try:
	import requests  # type: ignore
except Exception as exc:  # pragma: no cover
	raise SystemExit(
		"Das Paket 'requests' wird benötigt. Installiere es z.B. mit: pip install requests\n"
		f"Fehler: {exc}"
	)


API_BASES = (
	"https://api.deutsche-digitale-bibliothek.de/2",
	"https://api-q1.deutsche-digitale-bibliothek.de/2",
)


def _normalize_api_base(value: str) -> str:
	v = (value or "").strip()
	if not v:
		return API_BASES[0]
	if v.startswith("http://") or v.startswith("https://"):
		return v.rstrip("/")
	# Hostname ohne Schema
	return f"https://{v.rstrip('/')}"


def _search_url(api_base: str) -> str:
	return f"{api_base}/search/index/search/select"


def _item_url_tmpl(api_base: str) -> str:
	return f"{api_base}/items/{{id}}/edm"

DEFAULT_ROWS = 100_000
DEFAULT_THREADS = 16
DEFAULT_ITEM_TIMEOUT = 30.0
DEFAULT_SOLR_TIMEOUT = 180.0


def _fmt_int_de(value: int) -> str:
	# Python nutzt in f"{n:,}" das Komma als Tausendertrennzeichen -> für DE zu '.' wechseln.
	return f"{value:,}".replace(",", ".")


def _fmt_float_de(value: float, decimals: int = 1) -> str:
	# Erst US-Format mit Tausender-Komma und Dezimal-Punkt, danach sauber tauschen.
	s = f"{value:,.{decimals}f}"
	return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


@dataclass
class Counters:
	total_ids: int = 0
	downloaded_ok: int = 0
	downloaded_empty: int = 0
	no_edm: int = 0
	http_errors: int = 0
	exceptions: int = 0
	retries: int = 0
	queued_to_zip: int = 0
	written_to_zip: int = 0
	zip_files_created: int = 0


def _configure_logger(log_path: str, verbose: bool) -> logging.Logger:
	logger = logging.getLogger("DDBdownloader")
	logger.setLevel(logging.DEBUG)

	formatter = logging.Formatter(
		fmt="%(asctime)s %(levelname)s %(threadName)s %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)

	file_handler = RotatingFileHandler(
		log_path,
		maxBytes=50 * 1024 * 1024,
		backupCount=5,
		encoding="utf-8",
	)
	file_handler.setLevel(logging.DEBUG)
	file_handler.setFormatter(formatter)
	logger.addHandler(file_handler)

	# WICHTIG: Statusanzeige läuft über stderr mit "\r" (ohne Newline).
	# Damit Log-Meldungen nicht in der gleichen Zeile landen, gehen Konsolen-Logs nach stdout.
	console = logging.StreamHandler(stream=sys.stdout)
	console.setLevel(logging.INFO if verbose else logging.WARNING)
	console.setFormatter(formatter)
	logger.addHandler(console)

	return logger


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(
		prog="DDBdownloader",
		description=(
			"Massendownload von EDM-XML über die API der Deutschen Digitalen Bibliothek. "
			"Ablauf: Solr-Suche -> IDs seitenweise sammeln -> parallel EDM laden -> in ZIP schreiben. "
			"Fehler/Timeouts werden geloggt; am Ende wird eine Statistik ausgegeben."
		),
	)
	p.add_argument("-q", "--query", required=True, help='Solr q-Parameter, z.B. "dataset_id:..."')
	p.add_argument(
		"-o",
		"--output",
		default="",
		help='Output ZIP, z.B. "output.zip" (Logdatei wird daneben als output.log angelegt). Im --head-only Modus optional.',
	)
	p.add_argument(
		"-b",
		"--batch",
		type=int,
		default=0,
		help=(
			"Maximale Anzahl XML-Dateien pro ZIP. 0 oder weglassen: alles in eine ZIP. "
			"Bei >0 werden output-1.zip, output-2.zip ... erstellt."
		),
	)
	p.add_argument(
		"--api",
		default=API_BASES[0],
		help=(
			"API Base-URL. Unterstützt z.B. https://api.deutsche-digitale-bibliothek.de/2 und "
			"https://api-q1.deutsche-digitale-bibliothek.de/2"
		),
	)
	p.add_argument(
		"--threads",
		type=int,
		default=DEFAULT_THREADS,
		help="Parallelität (Download-Threads, Default: 16)",
	)
	p.add_argument(
		"--rows",
		type=int,
		default=DEFAULT_ROWS,
		help="IDs pro Seite bei der Solr-Abfrage (Default: 100000)",
	)
	p.add_argument(
		"--timeout",
		type=float,
		default=DEFAULT_ITEM_TIMEOUT,
		help="HTTP Timeout in Sekunden für Item-Downloads (Default: 30)",
	)
	p.add_argument(
		"--solr-timeout",
		type=float,
		default=DEFAULT_SOLR_TIMEOUT,
		help="Read-Timeout in Sekunden für Solr-ID-Abfragen (Default: 180)",
	)
	p.add_argument(
		"--retries",
		type=int,
		default=4,
		help="Anzahl Retries pro Objekt bei Fehlern (Default: 4; zusätzlich zum ersten Versuch)",
	)
	p.add_argument(
		"--backoff",
		type=float,
		default=1.0,
		help="Backoff-Basis in Sekunden (Default: 1.0; exponentiell pro Retry)",
	)
	p.add_argument(
		"--verbose",
		action="store_true",
		help="Mehr Konsolen-Logs (zusätzlich zur Statusanzeige; Details stehen immer in output.log)",
	)
	p.add_argument(
		"--head-only",
		action="store_true",
		help=(
			"Nur HEAD-Requests senden (kein Download, kein ZIP). "
			"Prüft, wie viele IDs tatsächlich ein EDM besitzen. "
			"404/409 gelten als 'kein EDM'; andere Fehler bekommen Retries."
		),
	)
	return p.parse_args(list(argv) if argv is not None else None)


def _output_zip_name(base_output: str, index: int, use_split: bool) -> str:
	if not use_split:
		return base_output
	root, ext = os.path.splitext(base_output)
	if not ext:
		ext = ".zip"
	return f"{root}-{index}{ext}"


def _http_headers() -> Dict[str, str]:
	headers = {
		"Accept": "application/xml",
		"accept-profile": "https://www.deutsche-digitale-bibliothek.de/ns/europeana-edm-profile",
		"User-Agent": "DDBdownloader/1.0",
	}
	return headers


def _solr_fetch_ids(
	session: requests.Session,
	search_url: str,
	query: str,
	rows: int,
	timeout: float,
	retries: int,
	backoff: float,
	logger: logging.Logger,
) -> Tuple[str, int]:
	"""Liest alle IDs seitenweise ein und schreibt sie in eine Temp-Datei.

	Returns: (path_to_tempfile, total_count)
	"""
	tmp = tempfile.NamedTemporaryFile(prefix="ddb_ids_", suffix=".txt", delete=False, mode="w", encoding="utf-8")
	tmp_path = tmp.name

	start = 0
	total = None
	last_print = 0.0

	logger.info("Starte ID-Sammlung über Solr: rows=%s", rows)

	try:
		while True:
			if _stop_event.is_set():
				logger.info("_stop_event ist gesetzt - beende Solr-Schleife")
				sys.stderr.write("\n>>> _stop_event erkannt - fahre herunter\n")
				sys.stderr.flush()
				break
			params = {
				"q": query,
				"fl": "id",
				"sort": "id ASC",
				"start": start,
				"rows": rows,
				"wt": "json",
			}

			data = None
			for attempt in range(1, retries + 2):
				if _stop_event.is_set():
					break
				try:
					resp = session.get(
						search_url,
						params=params,
						timeout=(10.0, max(10.0, timeout)),
					)
					if _stop_event.is_set():
						break
					if resp.status_code == 200:
						data = resp.json()
						break

					if resp.status_code in (408, 429, 500, 502, 503, 504):
						if attempt <= retries:
							logger.warning(
								"Solr Retry %s/%s: HTTP %s; start=%s",
								attempt,
								retries,
								resp.status_code,
								start,
							)
							_sleep_backoff(backoff, attempt)
							continue
						logger.error(
							"Solr-Abfrage fehlgeschlagen nach Retries: HTTP %s; start=%s; body=%s",
							resp.status_code,
							start,
							resp.text[:2000],
						)
						resp.raise_for_status()

					logger.error(
						"Solr-Abfrage fehlgeschlagen: HTTP %s; start=%s; body=%s",
						resp.status_code,
						start,
						resp.text[:2000],
					)
					resp.raise_for_status()
				except (requests.Timeout, requests.ConnectionError, json.JSONDecodeError) as exc:
					if attempt <= retries:
						logger.warning(
							"Solr Retry %s/%s: %s; start=%s",
							attempt,
							retries,
							type(exc).__name__,
							start,
						)
						_sleep_backoff(backoff, attempt)
						continue
					logger.exception("Solr-Abfrage final fehlgeschlagen: start=%s", start)
					raise

			if data is None:
				raise RuntimeError(f"Solr-Abfrage lieferte keine Daten; start={start}")

			response = data.get("response") or {}
			if total is None:
				total = int(response.get("numFound") or 0)

			docs = response.get("docs") or []
			if not docs:
				break

			for d in docs:
				_id = d.get("id")
				if not _id:
					continue
				tmp.write(str(_id))
				tmp.write("\n")
				start += 1

			now = time.time()
			if now - last_print >= 1.0:
				last_print = now
				# Status in STDERR, ohne Log zu fluten
				if total is not None and total > 0:
					pct = min(100.0, (start / total) * 100.0)
					pct_s = _fmt_float_de(pct, 1).rjust(4)
					sys.stderr.write(
						f"\rIDs gelesen: {_fmt_int_de(start)}/{_fmt_int_de(total)} ({pct_s}%)"
					)
				else:
					sys.stderr.write(f"\rIDs gelesen: {_fmt_int_de(start)}")
				sys.stderr.flush()

			# Wenn numFound bekannt ist und wir alles haben: Abbruch
			if total is not None and start >= total:
				break

	finally:
		tmp.close()
		sys.stderr.write("\n")
		sys.stderr.flush()

	if total is None:
		total = start

	logger.info("ID-Sammlung beendet: total_ids=%s; tmp=%s", f"{start:,}", tmp_path)
	return tmp_path, start


def _iter_ids_from_file(path: str) -> Iterable[str]:
	with open(path, "r", encoding="utf-8") as f:
		for line in f:
			v = line.strip()
			if v:
				yield v


def _download_one(
	session: requests.Session,
	item_id: str,
	item_url_tmpl: str,
	headers: Dict[str, str],
	timeout: float,
) -> Tuple[int, bytes]:
	url = item_url_tmpl.format(id=item_id)
	resp = session.get(url, headers=headers, timeout=timeout)
	return resp.status_code, resp.content


def _setup_signal_handlers() -> None:
	"""Installiert SIGTERM- und SIGBREAK-Handler, die _stop_event setzen statt abzustürzen."""
	def _handle(signum, frame):  # noqa: ARG001
		_stop_event.set()
		print(f"\n>>> Signal {signum} empfangen - fahre Downloader herunter...", file=sys.stderr)
		sys.stderr.flush()

	for sig in (signal.SIGTERM,):
		try:
			signal.signal(sig, _handle)
		except (OSError, AttributeError, ValueError):
			pass

	# Windows: Ctrl+Break sendet SIGBREAK -> sonst KeyboardInterrupt
	sigbreak = getattr(signal, "SIGBREAK", None)
	if sigbreak is not None:
		try:
			signal.signal(sigbreak, _handle)
		except (OSError, AttributeError, ValueError):
			pass


def _sleep_backoff(base: float, attempt: int) -> None:
	# attempt: 1..N – in kleinen Schritten, damit _stop_event schnell bemerkt wird.
	delay = base * (2 ** (attempt - 1))
	deadline = time.monotonic() + delay
	while time.monotonic() < deadline:
		if _stop_event.is_set():
			return
		time.sleep(min(0.2, deadline - time.monotonic()))


def _status_loop(stop_event: threading.Event, counters: Counters, phase_getter, started_at: float) -> None:
	last_written = 0
	last_len = 0
	while not stop_event.is_set():
		time.sleep(1.0)
		phase = phase_getter()
		elapsed = max(0.001, time.time() - started_at)
		done = counters.downloaded_ok + counters.downloaded_empty + counters.http_errors + counters.exceptions
		rate = done / elapsed
		eta = None
		if counters.total_ids > 0 and rate > 0:
			remaining = max(0, counters.total_ids - done)
			eta = remaining / rate

		line = (
			f"\r[{phase}] done={_fmt_int_de(done)}/{_fmt_int_de(counters.total_ids)} "
			f"ok={_fmt_int_de(counters.downloaded_ok)} empty={_fmt_int_de(counters.downloaded_empty)} "
			f"http={_fmt_int_de(counters.http_errors)} exc={_fmt_int_de(counters.exceptions)} "
			f"retries={_fmt_int_de(counters.retries)} zip={_fmt_int_de(counters.written_to_zip)} "
			f"rate={_fmt_float_de(rate, 1)}/s"
		)
		if eta is not None:
			line += f" ETA={_fmt_float_de(eta/60, 1)} min"

		# Auf 200 Zeichen begrenzen, aber alte Restzeichen überschreiben.
		line = line[:200]
		pad = " " * max(0, last_len - len(line))
		last_len = len(line)

		# Wenn nichts passiert, trotzdem nicht flackern – aber Status ist gewünscht.
		if counters.written_to_zip != last_written:
			last_written = counters.written_to_zip
		sys.stderr.write(line + pad)
		sys.stderr.flush()

	# Statuszeile sauber beenden
	sys.stderr.write("\n")
	sys.stderr.flush()


class ZipRotatingWriter:
	def __init__(self, base_output: str, batch_size: int, logger: logging.Logger):
		self.base_output = base_output
		self.batch_size = batch_size
		self.use_split = bool(batch_size and batch_size > 0)
		self.logger = logger

		self._zip_index = 1
		self._zip = None  # type: Optional[zipfile.ZipFile]
		self._zip_count = 0

	def _open_new_zip(self) -> None:
		if self._zip is not None:
			self._zip.close()
			self._zip = None

		zip_name = _output_zip_name(self.base_output, self._zip_index, self.use_split)
		os.makedirs(os.path.dirname(os.path.abspath(zip_name)) or ".", exist_ok=True)
		self._zip = zipfile.ZipFile(zip_name, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9)
		self._zip_count = 0
		self.logger.info("ZIP geöffnet: %s", zip_name)

	def write_xml(self, item_id: str, xml_bytes: bytes) -> None:
		if self._zip is None:
			self._open_new_zip()

		# Rotation
		if self.use_split and self._zip_count >= self.batch_size:
			self._zip_index += 1
			self._open_new_zip()

		arcname = f"{item_id}.xml"
		assert self._zip is not None, "ZIP file is not open"
		self._zip.writestr(arcname, xml_bytes)
		self._zip_count += 1

	def close(self) -> None:
		if self._zip is not None:
			self._zip.close()
			self._zip = None

	@property
	def zip_index(self) -> int:
		return self._zip_index


def main(argv: Optional[Iterable[str]] = None) -> int:
	_stop_event.clear()
	_setup_signal_handlers()

	args = _parse_args(argv)

	if not args.head_only and not args.output:
		print("Fehler: -o/--output ist erforderlich (außer bei --head-only).", file=sys.stderr)
		return 2

	if args.output:
		log_path = os.path.splitext(args.output)[0] + ".log"
	else:
		log_path = "ddbdownloader.log"

	logger = _configure_logger(log_path, args.verbose)

	api_base = _normalize_api_base(args.api)
	search_url = _search_url(api_base)
	item_url_tmpl = _item_url_tmpl(api_base)

	logger.info("API: %s", api_base)

	# Schritt 1: IDs einsammeln
	with requests.Session() as session:
		# Für Solr ist JSON praktischer.
		session.headers.update({"Accept": "application/json"})
		try:
			ids_path, total_ids = _solr_fetch_ids(
				session=session,
				search_url=search_url,
				query=args.query,
				rows=args.rows,
				timeout=args.solr_timeout,
				retries=args.retries,
				backoff=args.backoff,
				logger=logger,
			)
		except Exception:
			logger.exception("Abbruch bei ID-Sammlung")
			return 2

	print(f"Gefundene IDs: {_fmt_int_de(total_ids)}")

	if _stop_event.is_set():
		logger.info("Abbruch durch Stop-Signal nach ID-Sammlung.")
		try:
			os.unlink(ids_path)
		except Exception:
			pass
		return 130

	# Schritt 2: Download + ZIP
	counters = Counters(total_ids=total_ids)
	counters_lock = threading.Lock()
	headers = _http_headers()
	q_items: "queue.Queue[Optional[Tuple[str, bytes]]]" = queue.Queue(maxsize=5000)
	writer_stop = threading.Event()
	phase = {"value": "download"}

	started_at = time.time()
	status_stop = threading.Event()
	status_thread = threading.Thread(
		target=_status_loop,
		name="status",
		args=(status_stop, counters, lambda: phase["value"], started_at),
		daemon=True,
	)
	status_thread.start()

	def writer_worker() -> None:
		zw = ZipRotatingWriter(args.output, args.batch, logger)
		try:
			# mindestens eine ZIP als „created“ zählen, sobald sie geöffnet wird
			while True:
				item = q_items.get()
				if item is None:
					break
				item_id, xml_bytes = item
				try:
					zw.write_xml(item_id, xml_bytes)
					with counters_lock:
						counters.written_to_zip += 1
				except Exception:
					logger.exception("ZIP write failed für id=%s", item_id)
					with counters_lock:
						counters.exceptions += 1
		finally:
			try:
				zw.close()
				counters.zip_files_created = zw.zip_index if (args.batch and args.batch > 0) else (1 if counters.written_to_zip > 0 else 0)
			except Exception:
				logger.exception("Fehler beim Schließen der ZIP")

	writer_thread: Optional[threading.Thread] = None
	if not args.head_only:
		writer_thread = threading.Thread(target=writer_worker, name="zip-writer", daemon=True)
		writer_thread.start()

	# Per-Thread Session (requests.Session ist nicht offiziell thread-safe)
	thread_local = threading.local()

	def get_session() -> requests.Session:
		s = getattr(thread_local, "session", None)
		if s is None:
			s = requests.Session()
			thread_local.session = s
		return s

	def download_task(item_id: str) -> None:
		if _stop_event.is_set():
			return
		nonlocal counters
		s = get_session()

		if args.head_only:
			url = item_url_tmpl.format(id=item_id)
			for attempt in range(1, args.retries + 2):
				try:
					resp = s.head(url, headers=headers, timeout=args.timeout)
					sc = resp.status_code
					if sc == 200:
						with counters_lock:
							counters.downloaded_ok += 1
						return
					if sc in (404, 409):
						with counters_lock:
							counters.no_edm += 1
						logger.debug("Kein EDM (HTTP %s): id=%s", sc, item_id)
						return
					if sc in (408, 429, 500, 502, 503, 504):
						with counters_lock:
							counters.retries += 1
						if attempt <= args.retries:
							logger.warning("HEAD Retry %s/%s: HTTP %s id=%s", attempt, args.retries, sc, item_id)
							_sleep_backoff(args.backoff, attempt)
							continue
					with counters_lock:
						counters.http_errors += 1
					logger.error("HEAD HTTP Fehler %s: id=%s", sc, item_id)
					return
				except (requests.Timeout, requests.ConnectionError) as exc:
					with counters_lock:
						counters.retries += 1
					if attempt <= args.retries:
						logger.warning("HEAD Retry %s/%s: %s id=%s", attempt, args.retries, type(exc).__name__, item_id)
						_sleep_backoff(args.backoff, attempt)
						continue
					with counters_lock:
						counters.exceptions += 1
					logger.exception("HEAD Netzwerkfehler (final): id=%s", item_id)
					return
				except Exception:
					with counters_lock:
						counters.exceptions += 1
					logger.exception("HEAD Unerwarteter Fehler: id=%s", item_id)
					return
			return

		for attempt in range(1, args.retries + 2):
			try:
				status_code, body = _download_one(
					s,
					item_id,
					item_url_tmpl=item_url_tmpl,
					headers=headers,
					timeout=args.timeout,
				)
				if status_code == 200:
					if not body or len(body.strip()) == 0:
						with counters_lock:
							counters.downloaded_empty += 1
						logger.warning("Leere Antwort: id=%s", item_id)
						return
					# Minimaler Schutz gegen HTML-Fehlerseiten als 200
					if body.lstrip().startswith(b"<html") or body.lstrip().startswith(b"<!DOCTYPE html"):
						with counters_lock:
							counters.http_errors += 1
						logger.error("Unerwartete HTML-Antwort (200): id=%s", item_id)
						return

					q_items.put((item_id, body))
					with counters_lock:
						counters.queued_to_zip += 1
						counters.downloaded_ok += 1
					return

				# Kein EDM vorhanden – kein Retry
				if status_code in (404, 409):
					with counters_lock:
						counters.no_edm += 1
					logger.debug("Kein EDM (HTTP %s): id=%s", status_code, item_id)
					return

				# typische transient errors
				if status_code in (408, 429, 500, 502, 503, 504):
					with counters_lock:
						counters.retries += 1
					if attempt <= args.retries:
						logger.warning("Retry %s/%s: HTTP %s id=%s", attempt, args.retries, status_code, item_id)
						_sleep_backoff(args.backoff, attempt)
						continue

				with counters_lock:
					counters.http_errors += 1
				logger.error("HTTP Fehler %s: id=%s", status_code, item_id)
				return

			except (requests.Timeout, requests.ConnectionError) as exc:
				with counters_lock:
					counters.retries += 1
				if attempt <= args.retries:
					logger.warning("Retry %s/%s: %s id=%s", attempt, args.retries, type(exc).__name__, item_id)
					_sleep_backoff(args.backoff, attempt)
					continue
				with counters_lock:
					counters.exceptions += 1
				logger.exception("Netzwerkfehler (final): id=%s", item_id)
				return
			except Exception:
				with counters_lock:
					counters.exceptions += 1
				logger.exception("Unerwarteter Fehler: id=%s", item_id)
				return

	try:
		# IDs streamen (ohne in RAM zu laden)
		from concurrent.futures import FIRST_COMPLETED, wait

		max_workers = max(1, int(args.threads))
		max_outstanding = max_workers * 200

		with ThreadPoolExecutor(max_workers=max_workers) as pool:
			futures = set()
			for _id in _iter_ids_from_file(ids_path):
				if _stop_event.is_set():
					break
				futures.add(pool.submit(download_task, _id))
				if len(futures) >= max_outstanding:
					done, futures = wait(futures, return_when=FIRST_COMPLETED)
					# Exceptions sind bereits im Worker geloggt; hier nur Future freigeben.
					for f in done:
						try:
							f.result()
						except Exception:
							pass

			if futures:
				done, _ = wait(futures)
				for f in done:
					try:
						f.result()
					except Exception:
						pass

	except KeyboardInterrupt:
		_stop_event.set()
		logger.info("Abbruch durch KeyboardInterrupt.")
	finally:
		# Writer in jedem Fall (auch bei Abbruch) sauber beenden
		if writer_thread is not None:
			q_items.put(None)
			writer_thread.join()
		phase["value"] = "final"
		status_stop.set()
		try:
			status_thread.join(timeout=2.0)
		except Exception:
			pass
		try:
			os.unlink(ids_path)
		except Exception:
			logger.warning("Konnte Temp-ID-Datei nicht löschen: %s", ids_path)

	# Abschluss
	sys.stderr.write("\n")
	elapsed = max(0.001, time.time() - started_at)
	done = counters.downloaded_ok + counters.downloaded_empty + counters.no_edm + counters.http_errors + counters.exceptions
	rate = done / elapsed

	if args.head_only:
		print(f"\nErgebnis HEAD-Prüfung:")
		print(f"  Gesamt geprüft : {_fmt_int_de(done)}")
		print(f"  Mit EDM (200)  : {_fmt_int_de(counters.downloaded_ok)}")
		print(f"  Ohne EDM (404/409): {_fmt_int_de(counters.no_edm)}")
		print(f"  HTTP-Fehler     : {_fmt_int_de(counters.http_errors)}")
		print(f"  Exceptions      : {_fmt_int_de(counters.exceptions)}")
		print(f"  Retries         : {_fmt_int_de(counters.retries)}")
		print(f"  Durchsatz       : {_fmt_float_de(rate, 1)}/s")

	stats = {
		"mode": "head-only" if args.head_only else "download",
		"api_base": api_base,
		"total_ids": counters.total_ids,
		"done": done,
		"with_edm": counters.downloaded_ok,
		"without_edm": counters.no_edm,
		"downloaded_ok": counters.downloaded_ok,
		"downloaded_empty": counters.downloaded_empty,
		"http_errors": counters.http_errors,
		"exceptions": counters.exceptions,
		"retries": counters.retries,
		"written_to_zip": counters.written_to_zip,
		"zip_files_created": counters.zip_files_created,
		"elapsed_seconds": elapsed,
		"rate_per_second": rate,
		"output": args.output,
		"batch": args.batch,
		"threads": args.threads,
	}

	if not args.head_only:
		print("\nStatistik:")
	else:
		print("\nDetails (JSON):")
	print(json.dumps(stats, indent=2, ensure_ascii=False))
	logger.info("Fertig. Statistik: %s", json.dumps(stats, ensure_ascii=False))

	# Returncode: 0 wenn alles ok; 1 wenn irgendwas schief ging
	if counters.http_errors or counters.exceptions:
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

 
