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

		# Query
		tk.Label(frm, text="Query (-q):").grid(row=0, column=0, sticky="w")
		self.var_query = tk.StringVar(value='dataset_id:')
		tk.Entry(frm, textvariable=self.var_query, width=80).grid(row=0, column=1, sticky="we", padx=(5, 5))

		# Output
		tk.Label(frm, text="Output ZIP (-o):").grid(row=1, column=0, sticky="w", pady=(6, 0))
		self.var_output = tk.StringVar(value=os.path.join(SCRIPT_DIR, "output.zip"))
		tk.Entry(frm, textvariable=self.var_output, width=80).grid(row=1, column=1, sticky="we", padx=(5, 5), pady=(6, 0))
		tk.Button(frm, text="…", width=3, command=self._browse_output).grid(row=1, column=2, pady=(6, 0))

		# Batch + Threads
		tk.Label(frm, text="Batch (-b):").grid(row=2, column=0, sticky="w", pady=(6, 0))
		self.var_batch = tk.StringVar(value="0")
		batch_entry = tk.Entry(frm, textvariable=self.var_batch, width=12)
		batch_entry.grid(row=2, column=1, sticky="w", padx=(5, 0), pady=(6, 0))

		# Threads sind bewusst nicht konfigurierbar – der Downloader nutzt Default=16.

		# Buttons
		btns = tk.Frame(frm)
		btns.grid(row=3, column=1, sticky="w", pady=(10, 0))
		self.btn_start = tk.Button(btns, text="Start", width=12, command=self._start)
		self.btn_start.pack(side=tk.LEFT)
		self.btn_stop = tk.Button(btns, text="Stop", width=12, state=tk.DISABLED, command=self._stop)
		self.btn_stop.pack(side=tk.LEFT, padx=(8, 0))

		# Status line
		self.lbl_status = tk.Label(self, text=self.last_status, anchor="w")
		self.lbl_status.pack(fill=tk.X, padx=10)

		# Output text
		out_frame = tk.Frame(self)
		out_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

		self.txt = tk.Text(out_frame, wrap="none")
		self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

		scroll_y = tk.Scrollbar(out_frame, orient="vertical", command=self.txt.yview)
		scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
		self.txt.configure(yscrollcommand=scroll_y.set)

		# Make column 1 expand
		frm.grid_columnconfigure(1, weight=1)

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

	def _validate(self) -> tuple[str, str, int]:
		q = (self.var_query.get() or "").strip()
		out = (self.var_output.get() or "").strip()
		if not q:
			raise ValueError("Query (-q) ist leer.")
		if not out:
			raise ValueError("Output (-o) ist leer.")

		batch_raw = (self.var_batch.get() or "").strip()
		batch = int(batch_raw) if batch_raw else 0
		if batch < 0:
			raise ValueError("Batch (-b) muss >= 0 sein.")
		return q, out, batch

	def _start(self):
		if self.proc is not None:
			return

		try:
			q, out, batch = self._validate()
		except Exception as exc:
			messagebox.showerror("Ungültige Eingabe", str(exc))
			return

		cmd = _downloader_command() + [
			"-q",
			q,
			"-o",
			out,
		]
		if batch:
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
		try:
			self._append_text("\nStop angefordert…\n")
			p.terminate()
		except Exception:
			pass

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
