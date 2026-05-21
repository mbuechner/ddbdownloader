import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADER_PY = os.path.join(SCRIPT_DIR, "DDBdownloader.py")

API_BASES = [
	"https://api.deutsche-digitale-bibliothek.de/2",
	"https://api-q1.deutsche-digitale-bibliothek.de/2",
]


def _is_frozen() -> bool:
	return bool(getattr(sys, "frozen", False))


def _downloader_command() -> list[str]:
	# Im Release-Paket läuft die GUI als .exe (PyInstaller). Dann starten wir die
	# separat gebaute CLI-EXE im gleichen Ordner.
	if _is_frozen():
		base_dir = os.path.dirname(os.path.abspath(sys.executable))
		downloader_exe = os.path.join(base_dir, "DDBdownloader.exe")
		return [downloader_exe]

	# Development/Source-Mode: Python + Script
	return [sys.executable, DOWNLOADER_PY]


class App(tk.Tk):
	def __init__(self):
		super().__init__()
		self.title("DDBdownloader GUI")
		self.geometry("900x600")

		self.proc = None
		self.reader_thread = None
		self.msg_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
		self.last_status = "Bereit."

		self._build_ui()
		self.after(0, self._center_window)
		self.after(100, self._drain_queue)

	def _center_window(self):
		# Nach dem Layout zentrieren (funktioniert zuverlässig unter Windows).
		self.update_idletasks()
		w = self.winfo_width()
		h = self.winfo_height()
		sw = self.winfo_screenwidth()
		sh = self.winfo_screenheight()

		x = max(0, int((sw - w) / 2))
		y = max(0, int((sh - h) / 2))
		self.geometry(f"{w}x{h}+{x}+{y}")

	def _build_ui(self):
		frm = tk.Frame(self)
		frm.pack(fill=tk.X, padx=10, pady=10)

		# API
		tk.Label(frm, text="API:").grid(row=0, column=0, sticky="w")
		self.var_api = tk.StringVar(value=API_BASES[0])
		api_menu = tk.OptionMenu(frm, self.var_api, *API_BASES)
		api_menu.grid(row=0, column=1, sticky="w", padx=(5, 0))

		# Query
		tk.Label(frm, text="Query (-q):").grid(row=1, column=0, sticky="w", pady=(6, 0))
		self.var_query = tk.StringVar(value='dataset_id:')
		tk.Entry(frm, textvariable=self.var_query, width=80).grid(row=1, column=1, sticky="we", padx=(5, 5), pady=(6, 0))

		# Output
		tk.Label(frm, text="Output ZIP (-o):").grid(row=2, column=0, sticky="w", pady=(6, 0))
		self.var_output = tk.StringVar(value=os.path.join(SCRIPT_DIR, "output.zip"))
		out_entry = tk.Entry(frm, textvariable=self.var_output, width=80)
		out_entry.grid(row=2, column=1, sticky="we", padx=(5, 5), pady=(6, 0))
		out_btn = tk.Button(frm, text="…", width=3, command=self._browse_output)
		out_btn.grid(row=2, column=2, pady=(6, 0))
		self._output_widgets = [out_entry, out_btn]

		# Batch + Threads
		tk.Label(frm, text="Batchgröße ZIP (-b):").grid(row=3, column=0, sticky="w", pady=(6, 0))
		self.var_batch = tk.StringVar(value="0")
		batch_entry = tk.Entry(frm, textvariable=self.var_batch, width=12)
		batch_entry.grid(row=3, column=1, sticky="w", padx=(5, 0), pady=(6, 0))

		# HEAD-only Modus
		self.var_head_only = tk.BooleanVar(value=False)
		head_chk = tk.Checkbutton(
			frm,
			text="Kein Download, nur Statistik (--head-only)",
			variable=self.var_head_only,
			command=self._on_head_only_toggle,
		)
		head_chk.grid(row=4, column=1, sticky="w", padx=(5, 0), pady=(6, 0))

		# Threads sind bewusst nicht konfigurierbar – der Downloader nutzt Default=16.

		# Buttons
		btns = tk.Frame(frm)
		btns.grid(row=5, column=1, sticky="w", pady=(10, 0))
		self.btn_start = tk.Button(btns, text="Start", width=12, command=self._start)
		self.btn_start.pack(side=tk.LEFT)
		self.btn_stop = tk.Button(btns, text="Stopp", width=12, state=tk.DISABLED, command=self._stop)
		self.btn_stop.pack(side=tk.LEFT, padx=(8, 0))

		# Status line
		self.lbl_status = tk.Label(self, text=self.last_status, anchor="w")
		self.lbl_status.pack(fill=tk.X, padx=10)

		# Output text
		out_frame = tk.Frame(self)
		out_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
		out_frame.grid_rowconfigure(0, weight=1)
		out_frame.grid_columnconfigure(0, weight=1)

		self.txt = tk.Text(out_frame, wrap="none")
		self.txt.grid(row=0, column=0, sticky="nsew")

		scroll_y = tk.Scrollbar(out_frame, orient="vertical", command=self.txt.yview)
		scroll_y.grid(row=0, column=1, sticky="ns")
		scroll_x = tk.Scrollbar(out_frame, orient="horizontal", command=self.txt.xview)
		scroll_x.grid(row=1, column=0, sticky="ew")
		self.txt.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

		# Make column 1 expand
		frm.grid_columnconfigure(1, weight=1)

	def _on_head_only_toggle(self):
		"""Output- und Batch-Feld bei HEAD-only de-/aktivieren."""
		state = tk.DISABLED if self.var_head_only.get() else tk.NORMAL
		for w in self._output_widgets:
			w.configure(state=state)

	def _browse_output(self):
		path = filedialog.asksaveasfilename(
			title="Output ZIP auswählen",
			defaultextension=".zip",
			filetypes=[("ZIP", "*.zip"), ("Alle Dateien", "*")],
			initialfile=os.path.basename(self.var_output.get() or "output.zip"),
		)
		if path:
			self.var_output.set(path)

	def _append_text(self, s: str):
		self.txt.insert(tk.END, s)
		self.txt.see(tk.END)

	def _set_status(self, s: str):
		self.last_status = s
		self.lbl_status.configure(text=s)

	def _validate(self) -> tuple[str, str, int, str, bool]:
		api = (self.var_api.get() or "").strip()
		if not api:
			raise ValueError("API ist leer.")
		q = (self.var_query.get() or "").strip()
		out = (self.var_output.get() or "").strip()
		head_only = self.var_head_only.get()
		if not q:
			raise ValueError("Query (-q) ist leer.")
		if not head_only and not out:
			raise ValueError("Output (-o) ist leer (nur bei HEAD-only optional).")

		batch_raw = (self.var_batch.get() or "").strip()
		batch = int(batch_raw) if batch_raw else 0
		if batch < 0:
			raise ValueError("Batch (-b) muss >= 0 sein.")
		return q, out, batch, api, head_only

	def _start(self):
		if self.proc is not None:
			return

		try:
			q, out, batch, api, head_only = self._validate()
		except Exception as exc:
			messagebox.showerror("Ungültige Eingabe", str(exc))
			return

		cmd = _downloader_command() + [
			"--api",
			api,
			"-q",
			q,
		]
		if out:
			cmd += ["-o", out]
		if head_only:
			cmd += ["--head-only"]
		elif batch:
			cmd += ["-b", str(batch)]

		# Existenz-Check passend zum Modus
		if _is_frozen():
			if not os.path.exists(cmd[0]):
				messagebox.showerror(
					"Fehler",
					f"Nicht gefunden: {cmd[0]}\n\nHinweis: Im Release-ZIP muss DDBdownloader.exe neben der GUI-EXE liegen.",
				)
				return
		else:
			if not os.path.exists(DOWNLOADER_PY):
				messagebox.showerror("Fehler", f"Nicht gefunden: {DOWNLOADER_PY}")
				return

		self.txt.delete("1.0", tk.END)
		self._append_text(f"Starte: {' '.join(cmd)}\n")
		self._set_status("Starte Download…")

		# Start subprocess
		try:
			startupinfo = None
			creationflags = 0
			if os.name == "nt":
				# Verhindert das Öffnen eines Konsolenfensters (z.B. bei DDBdownloader.exe).
				creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
				startupinfo = subprocess.STARTUPINFO()
				startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
				startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)

			self.proc = subprocess.Popen(
				cmd,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
				text=True,
				bufsize=0,
				startupinfo=startupinfo,
				creationflags=creationflags,
			)
		except Exception as exc:
			self.proc = None
			messagebox.showerror("Fehler", f"Konnte Prozess nicht starten: {exc}")
			return

		self.btn_start.configure(state=tk.DISABLED)
		self.btn_stop.configure(state=tk.NORMAL)

		self.reader_thread = threading.Thread(target=self._reader_worker, daemon=True)
		self.reader_thread.start()

	def _stop(self):
		p = self.proc
		if p is None:
			return
		self.btn_stop.configure(state=tk.DISABLED)
		self.msg_queue.put(("line", "\nStopp angefordert (bis zu 15 Sekunden Wartezeit)…\n"))
		threading.Thread(target=self._stop_worker, args=(p,), daemon=True).start()

	def _stop_worker(self, p: subprocess.Popen) -> None:
		"""Beendet den Prozess kooperativ und notfalls erzwungen (portable Lösung)."""
		try:
			# Phase 1: Sanftes terminate() (Timeout: 10 s)
			# Funktioniert überall: sendet SIGTERM (Unix) oder TerminateProcess (Windows)
			self.msg_queue.put(("line", "Beende Prozess sauber…\n"))
			try:
				p.terminate()
				p.wait(timeout=10)
				self.msg_queue.put(("line", "Prozess sauber beendet.\n"))
				return
			except subprocess.TimeoutExpired:
				pass

			# Phase 2: Erzwungener Abbruch mit kill() (Timeout: 5 s)
			# Funktioniert überall: sendet SIGKILL (Unix) oder TerminateProcess (Windows)
			self.msg_queue.put(("line", "Erzwinge Abbruch…\n"))
			p.kill()
			p.wait(timeout=5)
			self.msg_queue.put(("line", "Prozess beendet.\n"))
		except subprocess.TimeoutExpired:
			self.msg_queue.put(("line", "Warnung: Prozess konnte nicht beendet werden.\n"))
		except Exception as exc:
			self.msg_queue.put(("line", f"Fehler beim Beenden: {exc}\n"))

	def _reader_worker(self):
		assert self.proc is not None
		p = self.proc

		def read_stream(stream, tag: str):
			# Liest auch \r-Statusupdates zuverlässig, indem wir chunk-basiert splitten.
			buf = ""
			while True:
				chunk = stream.read(1)
				if chunk == "":
					break
				buf += chunk
				# Status Updates werden oft per \r geschrieben
				while "\r" in buf or "\n" in buf:
					# Split on earliest delimiter
					r_pos = buf.find("\r")
					n_pos = buf.find("\n")
					candidates = [pos for pos in (r_pos, n_pos) if pos != -1]
					if not candidates:
						break
					pos = min(candidates)
					line = buf[:pos]
					delim = buf[pos]
					buf = buf[pos + 1 :]

					if delim == "\r":
						self.msg_queue.put(("status", line))
					else:
						self.msg_queue.put(("line", f"{line}\n"))

			# Flush remainder
			if buf:
				self.msg_queue.put(("line", f"{buf}\n"))

		threads = []
		if p.stdout is not None:
			threads.append(threading.Thread(target=read_stream, args=(p.stdout, "stdout"), daemon=True))
		if p.stderr is not None:
			threads.append(threading.Thread(target=read_stream, args=(p.stderr, "stderr"), daemon=True))
		for t in threads:
			t.start()

		# Wait process
		exit_code = p.wait()
		# Give reader threads a moment
		deadline = time.time() + 1.0
		for t in threads:
			remaining = max(0.0, deadline - time.time())
			t.join(timeout=remaining)

		self.msg_queue.put(("exit", str(exit_code)))

	def _drain_queue(self):
		try:
			while True:
				kind, payload = self.msg_queue.get_nowait()
				if kind == "line":
					self._append_text(payload)
				elif kind == "status":
					# Zeige die letzte Status-Zeile (ohne Log zu fluten)
					p = payload.strip()
					if p:
						self._set_status(p)
				elif kind == "exit":
					exit_code = int(payload)
					self._append_text(f"\nProzess beendet. Exit-Code: {exit_code}\n")
					self._set_status(f"Fertig. Exit-Code: {exit_code}")
					self.proc = None
					self.btn_start.configure(state=tk.NORMAL)
					self.btn_stop.configure(state=tk.DISABLED)
		except queue.Empty:
			pass
		finally:
			self.after(100, self._drain_queue)


def main() -> int:
	app = App()
	app.mainloop()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
