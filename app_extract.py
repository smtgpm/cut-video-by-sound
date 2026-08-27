"""
app_extract.py
Petit IDE Tkinter pour :
  - charger une vidéo
  - naviguer dedans (slider + aperçu image)
  - marquer un ou plusieurs extraits (début / fin)
  - extraire l'audio de chaque extrait (.wav) dans un dossier

Dépendances Python : opencv-python, pillow
Dépendance externe : ffmpeg doit être installé et accessible dans le PATH
"""

import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk


class VideoExtractorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Extracteur d'extraits audio")
        self.root.geometry("900x650")

        self.cap = None
        self.video_path = None
        self.fps = 25.0
        self.duration = 0.0
        self.clips = []  # liste de {"start": float, "end": float}
        self.pending_start = None

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Button(top, text="Ouvrir une vidéo", command=self.open_video).pack(side="left")
        self.lbl_path = ttk.Label(top, text="Aucune vidéo chargée")
        self.lbl_path.pack(side="left", padx=10)

        self.canvas = tk.Label(self.root, bg="black")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)

        nav = ttk.Frame(self.root)
        nav.pack(fill="x", padx=8, pady=4)

        self.time_var = tk.DoubleVar(value=0.0)
        self.slider = ttk.Scale(
            nav, from_=0, to=100, orient="horizontal",
            variable=self.time_var, command=self._on_slider_move
        )
        self.slider.pack(fill="x", side="top")

        self.lbl_time = ttk.Label(nav, text="00:00.000 / 00:00.000")
        self.lbl_time.pack(side="left")

        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=8, pady=4)

        ttk.Button(actions, text="Marquer début", command=self.mark_start).pack(side="left", padx=4)
        ttk.Button(actions, text="Marquer fin / ajouter extrait", command=self.mark_end).pack(side="left", padx=4)
        ttk.Button(actions, text="Reculer 1s", command=lambda: self.nudge(-1)).pack(side="left", padx=4)
        ttk.Button(actions, text="Avancer 1s", command=lambda: self.nudge(1)).pack(side="left", padx=4)
        ttk.Button(actions, text="Reculer 0.1s", command=lambda: self.nudge(-0.1)).pack(side="left", padx=4)
        ttk.Button(actions, text="Avancer 0.1s", command=lambda: self.nudge(0.1)).pack(side="left", padx=4)

        self.lbl_pending = ttk.Label(actions, text="Début en attente : aucun")
        self.lbl_pending.pack(side="left", padx=20)

        listframe = ttk.Frame(self.root)
        listframe.pack(fill="both", expand=False, padx=8, pady=4)

        ttk.Label(listframe, text="Extraits marqués :").pack(anchor="w")
        self.listbox = tk.Listbox(listframe, height=6)
        self.listbox.pack(fill="x", side="left", expand=True)
        ttk.Button(listframe, text="Supprimer sélection", command=self.remove_selected).pack(side="left", padx=6)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=10)
        ttk.Button(bottom, text="Extraire l'audio de tous les extraits", command=self.extract_all).pack(side="left")

    # ---------- Gestion vidéo ----------
    def open_video(self):
        path = filedialog.askopenfilename(
            title="Choisir une vidéo",
            filetypes=[("Vidéos", "*.mp4 *.mkv *.avi *.mov *.webm"), ("Tous les fichiers", "*.*")],
        )
        if not path:
            return
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Erreur", "Impossible d'ouvrir cette vidéo.")
            return
        self.cap = cap
        self.video_path = path
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        self.duration = frame_count / self.fps if self.fps else 0.0

        self.lbl_path.config(text=os.path.basename(path))
        self.slider.config(to=max(self.duration, 0.01))
        self.time_var.set(0.0)
        self.clips = []
        self.listbox.delete(0, "end")
        self.pending_start = None
        self._refresh_pending_label()
        self._show_frame_at(0.0)

    def _on_slider_move(self, _value):
        t = float(self.time_var.get())
        self._show_frame_at(t)

    def nudge(self, delta):
        if self.cap is None:
            return
        t = max(0.0, min(self.duration, float(self.time_var.get()) + delta))
        self.time_var.set(t)
        self._show_frame_at(t)

    def _show_frame_at(self, t_seconds):
        if self.cap is None:
            return
        frame_idx = int(t_seconds * self.fps)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = self.cap.read()
        if not ok:
            return
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        max_w, max_h = 860, 440
        scale = min(max_w / w, max_h / h)
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
        img = ImageTk.PhotoImage(Image.fromarray(frame))
        self.canvas.imgtk = img  # garder une référence, sinon Tkinter la garbage-collect
        self.canvas.config(image=img)
        self.lbl_time.config(text=f"{self._fmt(t_seconds)} / {self._fmt(self.duration)}")

    @staticmethod
    def _fmt(t):
        m = int(t // 60)
        s = t - m * 60
        return f"{m:02d}:{s:06.3f}"

    # ---------- Marquage des extraits ----------
    def mark_start(self):
        if self.cap is None:
            return
        self.pending_start = float(self.time_var.get())
        self._refresh_pending_label()

    def mark_end(self):
        if self.cap is None:
            return
        if self.pending_start is None:
            messagebox.showwarning("Attention", "Marque d'abord un début.")
            return
        end = float(self.time_var.get())
        start = self.pending_start
        if end <= start:
            messagebox.showwarning("Attention", "La fin doit être après le début.")
            return
        self.clips.append({"start": start, "end": end})
        self.listbox.insert("end", f"Extrait {len(self.clips)} : {self._fmt(start)} -> {self._fmt(end)}")
        self.pending_start = None
        self._refresh_pending_label()

    def _refresh_pending_label(self):
        txt = "aucun" if self.pending_start is None else self._fmt(self.pending_start)
        self.lbl_pending.config(text=f"Début en attente : {txt}")

    def remove_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        del self.clips[idx]
        self.listbox.delete(0, "end")
        for i, c in enumerate(self.clips, 1):
            self.listbox.insert("end", f"Extrait {i} : {self._fmt(c['start'])} -> {self._fmt(c['end'])}")

    # ---------- Extraction audio ----------
    def extract_all(self):
        if not self.clips:
            messagebox.showwarning("Attention", "Aucun extrait marqué.")
            return
        out_dir = filedialog.askdirectory(title="Dossier de sortie pour les .wav")
        if not out_dir:
            return
        base = os.path.splitext(os.path.basename(self.video_path))[0]
        errors = []
        for i, clip in enumerate(self.clips, 1):
            out_path = os.path.join(out_dir, f"{base}_extrait_{i}.wav")
            ok, msg = self._extract_audio_segment(self.video_path, clip["start"], clip["end"], out_path)
            if not ok:
                errors.append(msg)
        if errors:
            messagebox.showerror("Erreurs", "\n".join(errors))
        else:
            messagebox.showinfo("Terminé", f"{len(self.clips)} extrait(s) audio sauvegardé(s) dans :\n{out_dir}")

    @staticmethod
    def _extract_audio_segment(video_path, start, end, out_path):
        duration = end - start
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            "-vn", "-ac", "1", "-ar", "22050",
            "-acodec", "pcm_s16le",
            out_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return False, f"ffmpeg a échoué pour {out_path} :\n{result.stderr[-500:]}"
            return True, "ok"
        except FileNotFoundError:
            return False, "ffmpeg introuvable. Installe-le et assure-toi qu'il est dans le PATH."


def main():
    root = tk.Tk()
    VideoExtractorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
