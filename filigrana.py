#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF Sporca v3 — Filigrana + effetti scansione realistica
Novità v3:
  • Anti-aliasing rotazione (LANCZOS su sovracampionamento 2×) → no aliasing
  • Controllo anti-aliasing nella UI (checkbox + intensità)
  • Progresso granulare per pagina tramite Queue inter-processo
  • Zoom anteprima con rotella del mouse
  • Effetti scansione migliorati: vignettatura, micro-ondulazione, macchie carta,
    deformazione prospettica lieve, grain fotografico
  • Dialogo finale con lista file cliccabili
  • Barra progresso per pagina + etichetta percentuale
"""

import os, sys, json, threading, queue, random, io, multiprocessing, time
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from tkinter import TclError
from tkinterdnd2 import DND_FILES, TkinterDnD

import numpy as np
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageTk, ImageFilter, ImageEnhance, ImageDraw
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
import fitz

CONFIG_FILE = os.path.join(os.path.dirname(sys.argv[0]), 'config.json')
A4_WIDTH, A4_HEIGHT = A4
MM_TO_PT = 72.0 / 25.4


# ═══════════════════════════════════════════════════════════════════════════════
#  FUNZIONI ELABORAZIONE
# ═══════════════════════════════════════════════════════════════════════════════

def modifica_opacita(img: Image.Image, opacita: float) -> Image.Image:
    if opacita >= 1.0:
        return img
    img = img.convert('RGBA')
    r, g, b, a = img.split()
    a = a.point(lambda i: int(i * opacita))
    return Image.merge('RGBA', (r, g, b, a))


def calcola_dimensioni(img, mode, scale, pw, ph):
    ri = img.width / img.height
    rp = pw / ph
    if mode == 'cover':
        if ri > rp:
            h = ph; w = h * ri
        else:
            w = pw; h = w / ri
    else:
        if ri > rp:
            w = pw; h = w / ri
        else:
            h = ph; w = h * ri
    return w * scale, h * scale


def genera_overlay(img_water, dx_mm, dy_mm, ang, w_pt, h_pt) -> bytes:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    cx, cy = A4_WIDTH / 2, A4_HEIGHT / 2
    c.saveState()
    c.translate(cx + dx_mm * MM_TO_PT, cy + dy_mm * MM_TO_PT)
    c.rotate(ang)
    c.translate(-w_pt / 2, -h_pt / 2)
    c.drawImage(ImageReader(img_water), 0, 0, width=w_pt, height=h_pt, mask='auto')
    c.restoreState()
    c.showPage(); c.save()
    return buf.getvalue()


# ─── Rotazione con anti-aliasing reale ────────────────────────────────────────
def ruota_con_antialiasing(img: Image.Image, angolo: float,
                            forza: int = 2) -> Image.Image:
    """
    Ruota con anti-aliasing corretto.

    PERCHÉ il sovracampionamento sull'immagine già rasterizzata non basta:
    se l'immagine arriva con bordi già scalettati (rasterizzata a 150 DPI),
    ingrandire e ridurre non rimuove quei gradini — li sfuma appena.

    La tecnica giusta qui è diversa:
    1. Prima sfumiamo leggermente l'immagine prima della rotazione
       (pre-blur di 0.4px) — attenua i bordi duri pixel-level
    2. Ruotiamo con BICUBIC (unico filtro interpolante accettato da rotate)
    3. Se forza>=2, eseguiamo un resize upscale+downscale sul risultato
       per smussare ulteriormente i bordi del foglio ruotato
    4. Post-blur minimale (0.3px) per eliminare residui di scalettatura

    Nota: LANCZOS è accettato SOLO da resize(), MAI da rotate().
    """
    if angolo == 0:
        return img

    # Step 1: pre-blur lieve per ammorbidire pixel boundaries prima della rotazione
    if forza >= 2:
        img = img.filter(ImageFilter.GaussianBlur(radius=0.4))

    # Step 2: rotazione BICUBIC (il migliore disponibile per rotate())
    rotated = img.rotate(angolo, expand=True,
                         fillcolor=(255, 255, 255),
                         resample=Image.Resampling.BICUBIC)

    if forza <= 1:
        return rotated

    # Step 3: se forza>=2, upscale 2× → blur lieve → downscale LANCZOS
    # Questo è il vero anti-aliasing sui bordi obliqui del foglio
    up_w = rotated.width  * 2
    up_h = rotated.height * 2
    big  = rotated.resize((up_w, up_h), Image.Resampling.BILINEAR)
    # Blur a metà pixel nella versione upscaled = 1px nel risultato finale
    big  = big.filter(ImageFilter.GaussianBlur(radius=0.6))
    # Riduzione con LANCZOS: kernel a 8 tap, ottimo per downscaling
    out  = big.resize((rotated.width, rotated.height), Image.Resampling.LANCZOS)

    # Step 4: se forza>=3, secondo giro upscale per qualità massima
    if forza >= 3:
        up2 = out.resize((out.width * 2, out.height * 2), Image.Resampling.BILINEAR)
        up2 = up2.filter(ImageFilter.GaussianBlur(radius=0.5))
        out = up2.resize((out.width, out.height), Image.Resampling.LANCZOS)

    return out


# ─── Effetti scansione realistica ─────────────────────────────────────────────
def applica_effetti(img: Image.Image, p: dict) -> Image.Image:
    arr = np.array(img.convert('RGB'), dtype=np.float32)

    # 1. Rumore gaussiano (grain pellicola) ── NumPy vettoriale
    if p.get('noise_enabled'):
        sigma = p.get('noise_intensity', 0.1) * 255
        arr += np.random.normal(0, sigma, arr.shape).astype(np.float32)
        arr  = np.clip(arr, 0, 255)

    # 2. Grain fotografico (texture diversa dal semplice gaussiano)
    if p.get('grain_enabled'):
        intens = p.get('grain_intensity', 0.05) * 255
        h, w   = arr.shape[:2]
        # Pattern di grain con luma-weighting (più visibile nelle mezzatinte)
        luma   = (0.299*arr[:,:,0] + 0.587*arr[:,:,1] + 0.114*arr[:,:,2]) / 255
        weight = 4 * luma * (1 - luma)           # massimo a luma=0.5
        grain  = np.random.normal(0, 1, (h, w)) * weight[:, :] * intens
        for c in range(3):
            arr[:,:,c] = np.clip(arr[:,:,c] + grain, 0, 255)

    img = Image.fromarray(arr.astype(np.uint8), 'RGB')

    # 3. Sfocatura (messa a fuoco imperfetta)
    if p.get('blur_enabled'):
        img = img.filter(ImageFilter.GaussianBlur(radius=p.get('blur_radius', 1.0)))

    # 4. Contrasto/luminosità casuale (variazione scansione)
    if p.get('contrast_enabled'):
        i2 = p.get('contrast_intensity', 0.2)
        img = ImageEnhance.Contrast(img).enhance(
            1.0 + random.uniform(-i2, i2))
        img = ImageEnhance.Brightness(img).enhance(
            1.0 + random.uniform(-i2/2, i2/2))

    # 5. Vignettatura (bordi più scuri, effetto scanner reale)
    if p.get('vignette_enabled'):
        strength = p.get('vignette_strength', 0.3)
        w, h = img.size
        arr2 = np.array(img, dtype=np.float32)
        Y, X = np.ogrid[:h, :w]
        cx, cy = w/2, h/2
        dist  = np.sqrt(((X-cx)/(cx))**2 + ((Y-cy)/(cy))**2)
        mask  = np.clip(1.0 - dist * strength, 0, 1)
        arr2 *= mask[:, :, np.newaxis]
        img   = Image.fromarray(np.clip(arr2, 0, 255).astype(np.uint8), 'RGB')

    # 6. Macchie/sporco carta (piccoli punti casuali)
    if p.get('dust_enabled'):
        n_dust  = int(p.get('dust_amount', 0.3) * 200)
        draw    = ImageDraw.Draw(img)
        w, h    = img.size
        for _ in range(n_dust):
            x = random.randint(0, w-1)
            y = random.randint(0, h-1)
            r = random.randint(1, 3)
            gray = random.randint(100, 200)
            draw.ellipse([x-r, y-r, x+r, y+r], fill=(gray, gray, gray))

    # 7. Micro-ondulazione (deformazione scansione) usando warp NumPy
    if p.get('warp_enabled'):
        ampl = p.get('warp_amplitude', 2.0)
        w, h = img.size
        arr3  = np.array(img)
        xs    = np.arange(w); ys = np.arange(h)
        XX, YY = np.meshgrid(xs, ys)
        freq   = random.uniform(0.005, 0.015)
        phase  = random.uniform(0, 2*np.pi)
        delta  = (np.sin(YY * freq + phase) * ampl).astype(int)
        XX2    = np.clip(XX + delta, 0, w-1)
        warped = arr3[YY, XX2]
        img    = Image.fromarray(warped.astype(np.uint8), 'RGB')

    # 8. Linee di scansione (vecchio scanner) ── NumPy slice
    if p.get('scanlines_enabled'):
        op   = p.get('scanlines_opacity', 0.1)
        arr4 = np.array(img, dtype=np.float32)
        arr4[::4, :, :] *= (1.0 - op)
        img  = Image.fromarray(np.clip(arr4, 0, 255).astype(np.uint8), 'RGB')

    return img


def rasterizza_pagina(pdf_bytes: bytes, dpi: int, max_rot: float,
                       aa_forza: int, effetti: dict,
                       fmt: str, q_jpeg: int, colore: str) -> Image.Image:
    """
    Rasterizza una pagina PDF con anti-aliasing reale.

    TECNICA CORRETTA per eliminare la scalettatura:
    PyMuPDF è un renderer vettoriale — se gli chiediamo DPI alti produce
    bordi perfettamente smooth. Quindi:
      1. Renderizziamo a DPI * aa_forza (es. 150 * 3 = 450 DPI)
      2. Riduciamo con LANCZOS al DPI target → anti-aliasing perfetto
         su tutto il contenuto (testi, linee, bordi)
      3. Applichiamo effetti sull'immagine già smooth
      4. Ruotiamo (i bordi del foglio saranno già smooth grazie ai passi precedenti)

    Questo è molto più efficace di qualunque post-processing sulla rotazione,
    perché l'aliasing nasce nella rasterizzazione, non nella rotazione.
    """
    # aa_forza=1 → DPI normale (veloce), =2 → 2× DPI, =3 → 3×, =4 → 4×
    # Per l'anteprima usiamo sempre almeno 2× per qualità decente
    moltiplicatore = max(1, aa_forza)
    dpi_render     = dpi * moltiplicatore

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(
        matrix=fitz.Matrix(dpi_render / 72.0, dpi_render / 72.0),
        alpha=False
    )
    img_hires = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()

    # Riduci al DPI target con LANCZOS → anti-aliasing su tutto il contenuto
    if moltiplicatore > 1:
        target_w = max(1, img_hires.width  // moltiplicatore)
        target_h = max(1, img_hires.height // moltiplicatore)
        img = img_hires.resize((target_w, target_h), Image.Resampling.LANCZOS)
    else:
        img = img_hires

    # Applica effetti sull'immagine già smooth
    img = applica_effetti(img, effetti)

    # Rotazione (bordi del foglio già smooth grazie all'alto DPI)
    if max_rot > 0:
        angolo = random.uniform(-max_rot, max_rot)
        # Con aa_forza>=2 già abbiamo un'immagine smooth, forza=1 basta per la rotazione
        img = ruota_con_antialiasing(img, angolo, forza=min(2, aa_forza))

    if colore == 'grigi':
        img = img.convert('L').convert('RGB')
    elif colore == 'bn':
        img = img.convert('L').point(lambda x: 0 if x < 128 else 255, '1').convert('RGB')

    if fmt == 'JPEG' and q_jpeg < 100:
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=q_jpeg, optimize=True)
        buf.seek(0); img = Image.open(buf); img.load()

    return img


# ─── Worker multiprocesso ─────────────────────────────────────────────────────
def _worker(args):
    (pdf_path, wm_bytes, wm_mode, out_dir,
     params, effetti, fmt, q_jpeg, colore, suffisso,
     progress_dict, task_id) = args

    try:
        wm = Image.open(io.BytesIO(wm_bytes)).convert(wm_mode)
        img_water = wm.copy()
        if params['opacity'] < 1.0:
            img_water = modifica_opacita(img_water, params['opacity'])

        tw, th = calcola_dimensioni(img_water, params['mode'],
                                    params['scale'], A4_WIDTH, A4_HEIGHT)
        reader  = PdfReader(pdf_path)
        n_pag   = len(reader.pages)
        name    = os.path.splitext(os.path.basename(pdf_path))[0]
        out_path= os.path.join(out_dir, f"{name}{suffisso}.pdf")
        c       = rl_canvas.Canvas(out_path, pagesize=A4)
        aa      = params.get('aa_forza', 2)

        for i, page in enumerate(reader.pages, 1):
            dx  = random.uniform(-params['max_dx'],    params['max_dx'])
            dy  = random.uniform(-params['max_dy'],    params['max_dy'])
            ang = random.uniform(-params['max_angle'], params['max_angle'])

            ov_page = PdfReader(
                io.BytesIO(genera_overlay(img_water, dx, dy, ang, tw, th))
            ).pages[0]
            page.merge_page(ov_page)

            tmp = PdfWriter(); tmp.add_page(page)
            buf = io.BytesIO(); tmp.write(buf)

            img_p = rasterizza_pagina(buf.getvalue(), params['dpi'],
                                      params['max_page_rotation'], aa,
                                      effetti, fmt, q_jpeg, colore)

            sc = min(A4_WIDTH/img_p.width, A4_HEIGHT/img_p.height)
            nw, nh = img_p.width*sc, img_p.height*sc
            x = (A4_WIDTH - nw)/2; y = (A4_HEIGHT - nh)/2

            if fmt == 'JPEG' and q_jpeg < 100:
                jb = io.BytesIO()
                img_p.save(jb, format='JPEG', quality=q_jpeg, optimize=True)
                jb.seek(0)
                c.drawImage(ImageReader(jb), x, y, width=nw, height=nh, mask='auto')
            else:
                c.drawImage(ImageReader(img_p), x, y, width=nw, height=nh, mask='auto')

            c.showPage()
            # Aggiorna progresso nella dict condivisa
            if progress_dict is not None:
                progress_dict[task_id] = (i, n_pag)

        c.save()
        return (pdf_path, True, out_path, n_pag)

    except Exception as e:
        import traceback
        return (pdf_path, False, str(e)+"\n"+traceback.format_exc(), 0)


# ═══════════════════════════════════════════════════════════════════════════════
#  DIALOGO FILE COMPLETATI
# ═══════════════════════════════════════════════════════════════════════════════

class DiaologoCompletato(Toplevel):
    """Finestra con lista dei file generati, cliccabili per aprirli."""
    def __init__(self, parent, risultati):
        super().__init__(parent)
        self.title("✅ Elaborazione completata")
        self.geometry("560x380")
        self.resizable(True, True)
        self.grab_set()

        ttk.Label(self, text="Elaborazione completata!",
                  font=('TkDefaultFont', 12, 'bold')).pack(pady=(15,5))
        ok_count  = sum(1 for r in risultati if r[1])
        err_count = len(risultati) - ok_count
        ttk.Label(self,
                  text=f"{ok_count} file creati con successo" +
                       (f"  |  {err_count} errori" if err_count else ""),
                  foreground='green' if not err_count else 'orange').pack(pady=2)

        ttk.Label(self, text="Clicca su un file per aprirlo:").pack(pady=(10,2))

        frame = ttk.Frame(self)
        frame.pack(fill=BOTH, expand=True, padx=15, pady=5)

        sb = ttk.Scrollbar(frame)
        sb.pack(side=RIGHT, fill=Y)
        lb = Listbox(frame, yscrollcommand=sb.set, activestyle='dotbox',
                     font=('TkFixedFont', 9), selectbackground='#0078d4',
                     selectforeground='white', height=10)
        lb.pack(fill=BOTH, expand=True)
        sb.config(command=lb.yview)

        self._paths = []
        for pdf_path, success, out_or_err, _ in risultati:
            if success:
                nome = os.path.basename(out_or_err)
                lb.insert(END, f"  ✓  {nome}")
                lb.itemconfig(END, foreground='#1a6e1a')
                self._paths.append(out_or_err)
            else:
                nome = os.path.basename(pdf_path)
                lb.insert(END, f"  ✗  {nome}  (errore)")
                lb.itemconfig(END, foreground='#cc0000')
                self._paths.append(None)

        lb.bind('<Double-Button-1>', lambda e: self._apri(lb.curselection()))
        lb.bind('<Return>',          lambda e: self._apri(lb.curselection()))

        tip = ttk.Label(self, text="Doppio clic o Invio per aprire",
                        font=('TkDefaultFont', 8), foreground='gray')
        tip.pack(pady=2)

        bf = ttk.Frame(self); bf.pack(pady=10)
        ttk.Button(bf, text="Apri tutti", command=self._apri_tutti).pack(side=LEFT, padx=5)
        ttk.Button(bf, text="Chiudi",     command=self.destroy).pack(side=LEFT, padx=5)

    def _apri(self, sel):
        if sel:
            p = self._paths[sel[0]]
            if p and os.path.isfile(p):
                try: os.startfile(p)
                except Exception as ex:
                    messagebox.showerror("Errore", str(ex), parent=self)

    def _apri_tutti(self):
        for p in self._paths:
            if p and os.path.isfile(p):
                try: os.startfile(p)
                except: pass


# ═══════════════════════════════════════════════════════════════════════════════
#  APPLICAZIONE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

class PdfSporcaApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Sporca v3 — Filigrana + Scansione Realistica")
        self.geometry("750x970")
        self.resizable(False, True)   # solo altezza ridimensionabile
        self.minsize(750, 500)

        self.pdf_files        = []
        self.watermark_path   = None
        self.watermark_image  = None
        self.thumbnail        = None
        self.config_data      = self._load_cfg()
        self.processing       = False
        self.preview_window   = None
        self.last_output_file = None
        self._mp_pool         = None
        self._progress_queue  = queue.Queue()   # progresso inter-thread
        self._results_accum   = []              # risultati accumulati

        self._build_ui()
        self._aggiorna_lista()
        self._carica_ultima_filigrana()
        self._load_settings()
        self.after(150, self._tick)

        self.drop_target_register(DND_FILES)
        self.dnd_bind('<<Drop>>', self._on_drop)
        self._traccia_modifiche()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─── Pool ─────────────────────────────────────────────────────────────────
    def _get_pool(self):
        if self._mp_pool is None:
            n = max(1, multiprocessing.cpu_count() - 1)
            ctx = multiprocessing.get_context('spawn')
            self._mp_pool = ctx.Pool(processes=n)
            self.log(f"⚡ Pool: {n} worker su {multiprocessing.cpu_count()} CPU")
        return self._mp_pool

    def _on_close(self):
        if self._mp_pool:
            self._mp_pool.terminate(); self._mp_pool.join()
        self.destroy()

    # ─── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        mc = Canvas(self, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient=VERTICAL, command=mc.yview)
        mc.configure(yscrollcommand=vsb.set)
        vsb.pack(side=RIGHT, fill=Y)
        mc.pack(side=LEFT, fill=BOTH, expand=True)
        inner = ttk.Frame(mc)
        # Larghezza fissa 742px per il frame interno (finestra 750 - scrollbar ~8)
        self._inner_win = mc.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: mc.configure(scrollregion=mc.bbox('all')))
        # Quando la finestra cambia altezza, aggiorna la scrollregion
        mc.bind('<Configure>', lambda e: mc.configure(
            scrollregion=mc.bbox('all')))

        # Scroll con rotellina sulla finestra principale
        def _scroll_main(e):
            mc.yview_scroll(int(-1*(e.delta/120)), "units")
        mc.bind('<MouseWheel>', _scroll_main)
        inner.bind('<MouseWheel>', _scroll_main)

        P = dict(fill=X, pady=4, padx=8)

        # ── PDF + Immagine ────────────────────────────────────────────────────
        top = ttk.Frame(inner); top.pack(fill=BOTH, expand=True, pady=5, padx=8)

        pdf_f = ttk.LabelFrame(top, text="PDF da elaborare", padding=5)
        pdf_f.pack(side=LEFT, fill=BOTH, expand=True, padx=2)
        self.pdf_listbox = Listbox(pdf_f, selectmode=EXTENDED, height=6)
        self.pdf_listbox.pack(fill=BOTH, expand=True, side=LEFT)
        psb = ttk.Scrollbar(pdf_f, orient=VERTICAL, command=self.pdf_listbox.yview)
        psb.pack(side=RIGHT, fill=Y)
        self.pdf_listbox.config(yscrollcommand=psb.set)
        pbf = ttk.Frame(pdf_f); pbf.pack(fill=X, pady=2)
        ttk.Button(pbf, text="Aggiungi", command=self._add_pdf).pack(side=LEFT, padx=1)
        ttk.Button(pbf, text="Rimuovi",  command=self._del_pdf).pack(side=LEFT, padx=1)
        ttk.Button(pbf, text="Svuota",   command=self._clr_pdf).pack(side=LEFT, padx=1)
        self.pdf_listbox.drop_target_register(DND_FILES)
        self.pdf_listbox.dnd_bind('<<Drop>>', self._on_drop_pdf)

        img_f = ttk.LabelFrame(top, text="Immagine filigrana", padding=5)
        img_f.pack(side=RIGHT, fill=BOTH, expand=True, padx=2)
        self.preview_label = Label(img_f, bg='#c8c8c8', relief=SUNKEN,
                                   width=20, height=8)
        self.preview_label.pack(pady=2, fill=BOTH, expand=True)
        self.img_info = StringVar(value="Nessuna immagine")
        ttk.Label(img_f, textvariable=self.img_info,
                  font=('TkDefaultFont', 8)).pack(pady=1)
        ibf = ttk.Frame(img_f); ibf.pack(fill=X, pady=2)
        ttk.Button(ibf, text="Scegli...", command=self._pick_img).pack(side=LEFT, padx=1)
        ttk.Button(ibf, text="Rimuovi",  command=self._rem_img).pack(side=LEFT, padx=1)
        self.preview_label.drop_target_register(DND_FILES)
        self.preview_label.dnd_bind('<<Drop>>', self._on_drop_img)

        ttk.Separator(inner, orient='horizontal').pack(**P)

        # ── Parametri filigrana ───────────────────────────────────────────────
        wf = ttk.LabelFrame(inner, text="Parametri filigrana", padding=5)
        wf.pack(**P)

        r1 = ttk.Frame(wf); r1.pack(fill=X, pady=2)
        self.scale_var = DoubleVar(value=1.0)
        self.mode_var  = StringVar(value='cover')
        self.opacity_var = DoubleVar(value=1.0)
        _lbl(r1,"Scala:"); ttk.Entry(r1,textvariable=self.scale_var,width=5).pack(side=LEFT,padx=2)
        _lbl(r1,"Modo:"); ttk.Combobox(r1,textvariable=self.mode_var,values=['cover','contain'],width=7,state='readonly').pack(side=LEFT,padx=2)
        _lbl(r1,"Opacità:"); ttk.Scale(r1,from_=0,to=1,orient=HORIZONTAL,variable=self.opacity_var,length=70).pack(side=LEFT,padx=2)
        ttk.Label(r1,textvariable=self.opacity_var,width=4).pack(side=LEFT)

        r2 = ttk.Frame(wf); r2.pack(fill=X, pady=2)
        self.dpi_var      = IntVar(value=150)
        self.dx_var       = DoubleVar(value=0.0)
        self.dy_var       = DoubleVar(value=0.0)
        _lbl(r2,"DPI:"); ttk.Entry(r2,textvariable=self.dpi_var,width=5).pack(side=LEFT,padx=2)
        _lbl(r2,"dx(mm):"); ttk.Entry(r2,textvariable=self.dx_var,width=5).pack(side=LEFT,padx=2)
        _lbl(r2,"dy(mm):"); ttk.Entry(r2,textvariable=self.dy_var,width=5).pack(side=LEFT,padx=2)

        r3 = ttk.Frame(wf); r3.pack(fill=X, pady=2)
        self.angle_var    = DoubleVar(value=0.0)
        self.page_rot_var = DoubleVar(value=1.0)
        _lbl(r3,"Angolo fil.(°):"); ttk.Entry(r3,textvariable=self.angle_var,width=5).pack(side=LEFT,padx=2)
        _lbl(r3,"Rot.pagina(°):"); ttk.Entry(r3,textvariable=self.page_rot_var,width=5).pack(side=LEFT,padx=2)

        # ── Anti-aliasing ─────────────────────────────────────────────────────
        r4 = ttk.Frame(wf); r4.pack(fill=X, pady=2)
        self.aa_var   = BooleanVar(value=True)
        self.aa_forza = IntVar(value=2)
        ttk.Checkbutton(r4, text="Anti-aliasing",
                        variable=self.aa_var).pack(side=LEFT)
        _lbl(r4,"  DPI interni (1×=veloce / 4×=ottimo):"); 
        ttk.Combobox(r4, textvariable=self.aa_forza,
                     values=[1,2,3,4], width=3,
                     state='readonly').pack(side=LEFT, padx=2)
        ttk.Label(r4, text="← rasterizza a DPI×N poi riduce con LANCZOS  (2× consigliato)",
                  foreground='#2a6496', font=('TkDefaultFont', 8)).pack(side=LEFT, padx=4)

        ttk.Separator(inner, orient='horizontal').pack(**P)

        # ── Effetti scansione ─────────────────────────────────────────────────
        ef = ttk.LabelFrame(inner, text="Effetti scansione realistica", padding=5)
        ef.pack(**P)

        # Definizioni effetti: (attr_enabled, attr_val, attr_max, label, val_lbl)
        self.noise_var        = BooleanVar(value=False)
        self.noise_intensity  = DoubleVar(value=0.05)
        self.noise_max        = DoubleVar(value=0.3)

        self.grain_var        = BooleanVar(value=False)
        self.grain_intensity  = DoubleVar(value=0.04)
        self.grain_max        = DoubleVar(value=0.2)

        self.blur_var         = BooleanVar(value=False)
        self.blur_radius      = DoubleVar(value=0.8)
        self.blur_max         = DoubleVar(value=3.0)

        self.contrast_var     = BooleanVar(value=False)
        self.contrast_intensity = DoubleVar(value=0.15)
        self.contrast_max     = DoubleVar(value=0.5)

        self.vignette_var     = BooleanVar(value=False)
        self.vignette_strength= DoubleVar(value=0.25)
        self.vignette_max     = DoubleVar(value=0.8)

        self.dust_var         = BooleanVar(value=False)
        self.dust_amount      = DoubleVar(value=0.2)
        self.dust_max         = DoubleVar(value=1.0)

        self.warp_var         = BooleanVar(value=False)
        self.warp_amplitude   = DoubleVar(value=1.5)
        self.warp_max         = DoubleVar(value=6.0)

        self.scanlines_var    = BooleanVar(value=False)
        self.scanlines_opacity= DoubleVar(value=0.08)
        self.scanlines_max    = DoubleVar(value=0.3)

        effetti_defs = [
            (self.noise_var,    self.noise_intensity, self.noise_max,       "Rumore (grain)",    "Intensità"),
            (self.grain_var,    self.grain_intensity, self.grain_max,       "Grain fotografico", "Intensità"),
            (self.blur_var,     self.blur_radius,     self.blur_max,        "Sfocatura",         "Raggio"),
            (self.contrast_var, self.contrast_intensity, self.contrast_max, "Contrasto/Lum.",    "Intensità"),
            (self.vignette_var, self.vignette_strength, self.vignette_max,  "Vignettatura",      "Forza"),
            (self.dust_var,     self.dust_amount,     self.dust_max,        "Sporco/Polvere",    "Quantità"),
            (self.warp_var,     self.warp_amplitude,  self.warp_max,        "Micro-ondulazione", "Ampiezza"),
            (self.scanlines_var,self.scanlines_opacity,self.scanlines_max,  "Linee scanner",     "Opacità"),
        ]

        # Usiamo grid dentro ef per allineamento colonne perfetto
        # col 0: checkbox+nome  col 1: label val  col 2: slider (expand)
        # col 3: entry valore   col 4: "Max:"  col 5: entry max
        ef.columnconfigure(2, weight=1)   # solo la colonna slider si espande

        self._sliders = {}
        for row_i, (chk_v, val_v, max_v, nome, val_lbl) in enumerate(effetti_defs):

            ttk.Checkbutton(ef, text=nome, variable=chk_v,
                            width=17).grid(row=row_i, column=0,
                                          sticky='w', padx=(2,0), pady=2)

            ttk.Label(ef, text=val_lbl+":",
                      width=8, anchor='e').grid(row=row_i, column=1,
                                                sticky='e', padx=(4,2))

            sl = ttk.Scale(ef, from_=0.0, to=max_v.get(),
                           orient=HORIZONTAL, variable=val_v)
            sl.grid(row=row_i, column=2, sticky='ew', padx=4, pady=2)

            val_entry = ttk.Entry(ef, textvariable=val_v, width=6)
            val_entry.grid(row=row_i, column=3, padx=2)

            ttk.Label(ef, text="Max:").grid(row=row_i, column=4,
                                            padx=(6, 1), sticky='e')

            max_entry = ttk.Entry(ef, textvariable=max_v, width=5)
            max_entry.grid(row=row_i, column=5, padx=(0, 4))

            # ── Max aggiorna lo slider IN TEMPO REALE mentre si digita ──────
            def _make_trace(s, mv, vv):
                def _trace(*_):
                    try:
                        new_max = float(mv.get())
                        if new_max <= 0:
                            return
                        s.config(to=new_max)
                        # Clamp il valore corrente se supera il nuovo max
                        if float(vv.get()) > new_max:
                            vv.set(round(new_max, 4))
                    except (ValueError, TclError):
                        pass
                return _trace

            max_v.trace_add('write', _make_trace(sl, max_v, val_v))
            # Mantieni anche Enter per compatibilità
            max_entry.bind('<Return>',
                           lambda e, s=sl, mv=max_v, vv=val_v:
                           self._upd_slider(s, mv, vv))

            self._sliders[id(max_v)] = (sl, max_v, val_v)

        ttk.Separator(inner, orient='horizontal').pack(**P)

        # ── Opzioni output ────────────────────────────────────────────────────
        oof = ttk.LabelFrame(inner, text="Opzioni output", padding=5)
        oof.pack(**P)

        rc = ttk.Frame(oof); rc.pack(fill=X, pady=2)
        _lbl(rc,"Colore:")
        self.colore_mode_var = StringVar(value='colore')
        for v,t in [('colore','Colore'),('grigi','Grigi'),('bn','B/N')]:
            ttk.Radiobutton(rc,text=t,variable=self.colore_mode_var,
                            value=v).pack(side=LEFT,padx=3)

        rs = ttk.Frame(oof); rs.pack(fill=X, pady=2)
        _lbl(rs,"Suffisso:")
        self.suffisso_var = StringVar(value='_s')
        ttk.Entry(rs, textvariable=self.suffisso_var, width=16).pack(side=LEFT,padx=5)

        ro = ttk.Frame(oof); ro.pack(fill=X, pady=2)
        _lbl(ro,"Output:")
        self.output_dir_var = StringVar(value=os.getcwd())
        self.output_entry = ttk.Entry(ro,textvariable=self.output_dir_var,width=22)
        self.output_entry.pack(side=LEFT,padx=2,fill=X,expand=True)
        self.btn_sfoglia = ttk.Button(ro,text="…",command=self._pick_dir,width=2)
        self.btn_sfoglia.pack(side=LEFT,padx=1)
        self.salva_in_origine_var = BooleanVar(value=False)
        self.salva_in_origine_var.trace_add('write', self._toggle_outdir)
        ttk.Checkbutton(ro,text="Origine",
                        variable=self.salva_in_origine_var).pack(side=LEFT,padx=4)
        ttk.Button(ro,text="📁",command=self._open_folder,width=2).pack(side=LEFT,padx=1)
        ttk.Button(ro,text="📄",command=self._open_last,  width=2).pack(side=LEFT,padx=1)

        ttk.Separator(inner, orient='horizontal').pack(**P)

        # ── Compressione ──────────────────────────────────────────────────────
        cf = ttk.LabelFrame(inner, text="Compressione immagine", padding=5)
        cf.pack(**P)
        rcomp = ttk.Frame(cf); rcomp.pack(fill=X, pady=2)
        _lbl(rcomp,"Formato:")
        self.img_format_var = StringVar(value='PNG')
        ttk.Combobox(rcomp,textvariable=self.img_format_var,
                     values=['PNG','JPEG'],width=5,
                     state='readonly').pack(side=LEFT,padx=2)
        _lbl(rcomp,"Qualità JPEG:")
        self.jpeg_quality_var = IntVar(value=85)
        ttk.Scale(rcomp,from_=1,to=100,orient=HORIZONTAL,
                  variable=self.jpeg_quality_var,length=100).pack(side=LEFT,padx=2)
        ttk.Entry(rcomp,textvariable=self.jpeg_quality_var,
                  width=3).pack(side=LEFT,padx=1)

        ttk.Separator(inner, orient='horizontal').pack(**P)

        # ── Info CPU ──────────────────────────────────────────────────────────
        n_cpu = multiprocessing.cpu_count()
        ttk.Label(inner,
                  text=f"⚡ {n_cpu} CPU — {max(1,n_cpu-1)} worker paralleli",
                  foreground='#2a7a2a',
                  font=('TkDefaultFont',9,'bold')).pack(pady=2)

        # ── Azioni ────────────────────────────────────────────────────────────
        af = ttk.Frame(inner); af.pack(fill=X, pady=5, padx=8)
        self.btn_avvia = ttk.Button(af, text="▶ Avvia",
                                    command=self._avvia)
        self.btn_avvia.pack(side=LEFT, padx=3)
        self.btn_stop = ttk.Button(af, text="⏹ Interrompi",
                                   command=self._stop, state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=3)
        self.btn_prev = ttk.Button(af, text="🔍 Anteprima",
                                   command=self._anteprima)
        self.btn_prev.pack(side=LEFT, padx=3)

        # Barra progresso + etichetta
        pbar_frame = ttk.Frame(inner)
        pbar_frame.pack(fill=X, padx=8, pady=3)
        self.progress = ttk.Progressbar(pbar_frame, orient=HORIZONTAL,
                                        mode='determinate')
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(0,5))
        self.prog_label = StringVar(value="")
        ttk.Label(pbar_frame, textvariable=self.prog_label,
                  width=14).pack(side=LEFT)

        # Log
        lf = ttk.LabelFrame(inner, text="Log", padding=5)
        lf.pack(fill=BOTH, expand=True, padx=8, pady=5)
        self.log_text = Text(lf, height=6, wrap=WORD)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        lsb = ttk.Scrollbar(lf, orient=VERTICAL, command=self.log_text.yview)
        lsb.pack(side=RIGHT, fill=Y)
        self.log_text.config(yscrollcommand=lsb.set)

    # ─── Helper slider ────────────────────────────────────────────────────────
    @staticmethod
    def _upd_slider(sl, max_v, val_v):
        try:
            m = max_v.get()
            sl.config(to=m)
            if val_v.get() > m: val_v.set(m)
        except: pass

    # ─── Traccia modifiche per anteprima auto ────────────────────────────────
    def _traccia_modifiche(self):
        for v in [self.scale_var, self.mode_var, self.opacity_var,
                  self.dpi_var, self.dx_var, self.dy_var,
                  self.angle_var, self.page_rot_var,
                  self.aa_var, self.aa_forza,
                  self.noise_var, self.noise_intensity,
                  self.grain_var, self.grain_intensity,
                  self.blur_var, self.blur_radius,
                  self.contrast_var, self.contrast_intensity,
                  self.vignette_var, self.vignette_strength,
                  self.dust_var, self.dust_amount,
                  self.warp_var, self.warp_amplitude,
                  self.scanlines_var, self.scanlines_opacity,
                  self.img_format_var, self.jpeg_quality_var,
                  self.colore_mode_var, self.suffisso_var]:
            try: v.trace_add('write', self._auto_prev)
            except: pass

    def _auto_prev(self, *_):
        if hasattr(self, '_after_prev'):
            self.after_cancel(self._after_prev)
        self._after_prev = self.after(700, self._update_prev_if_open)

    def _update_prev_if_open(self):
        if self.preview_window and self.preview_window.winfo_exists():
            self._anteprima(update_only=True)

    # ─── PDF list ────────────────────────────────────────────────────────────
    def _aggiorna_lista(self):
        self.pdf_listbox.delete(0, END)
        for f in self.pdf_files:
            self.pdf_listbox.insert(END, os.path.basename(f))

    def _add_pdf(self):
        files = filedialog.askopenfilenames(
            filetypes=[("PDF","*.pdf"),("Tutti","*.*")])
        for f in files:
            if f not in self.pdf_files: self.pdf_files.append(f)
        self._aggiorna_lista()

    def _del_pdf(self):
        for i in reversed(self.pdf_listbox.curselection()):
            del self.pdf_files[i]
        self._aggiorna_lista()

    def _clr_pdf(self):
        self.pdf_files.clear(); self._aggiorna_lista()

    # ─── Immagine ────────────────────────────────────────────────────────────
    def _pick_img(self):
        p = filedialog.askopenfilename(
            filetypes=[("PNG","*.png"),("Tutti","*.*")])
        if p: self._load_img(p)

    def _load_img(self, path):
        try:
            img = Image.open(path)
            self.watermark_image = img.copy()
            self.watermark_path  = path
            self.img_info.set(f"{os.path.basename(path)} ({img.width}×{img.height})")
            thumb = img.copy(); thumb.thumbnail((150,150))
            self.thumbnail = ImageTk.PhotoImage(thumb)
            self.preview_label.config(image=self.thumbnail)
            self._save_cfg()
            self.log(f"Immagine: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Errore", str(e))

    def _rem_img(self):
        self.watermark_image = None; self.watermark_path = None
        self.preview_label.config(image='')
        self.img_info.set("Nessuna immagine")
        self._save_cfg()

    # ─── Drag & drop ─────────────────────────────────────────────────────────
    @staticmethod
    def _parse(data):
        files, cur, br = [], '', False
        for ch in data:
            if ch=='{': br=True
            elif ch=='}': br=False; files.append(cur) if cur else None; cur=''
            elif ch==' ' and not br: files.append(cur) if cur else None; cur=''
            else: cur+=ch
        if cur: files.append(cur)
        return files

    def _on_drop_pdf(self, e):
        for f in self._parse(e.data):
            if f.lower().endswith('.pdf') and f not in self.pdf_files:
                self.pdf_files.append(f)
        self._aggiorna_lista()

    def _on_drop_img(self, e):
        for f in self._parse(e.data):
            if f.lower().endswith(('.png','.jpg','.jpeg','.bmp','.gif')):
                self._load_img(f); break

    def _on_drop(self, e):
        for f in self._parse(e.data):
            if f.lower().endswith('.pdf'):
                if f not in self.pdf_files: self.pdf_files.append(f)
            elif f.lower().endswith(('.png','.jpg','.jpeg','.bmp','.gif')):
                self._load_img(f)
        self._aggiorna_lista()

    # ─── Output dir ──────────────────────────────────────────────────────────
    def _pick_dir(self):
        p = filedialog.askdirectory()
        if p: self.output_dir_var.set(p); self._save_cfg()

    def _toggle_outdir(self, *_):
        s = 'disabled' if self.salva_in_origine_var.get() else 'normal'
        self.output_entry.config(state=s); self.btn_sfoglia.config(state=s)

    def _open_folder(self):
        p = self.output_dir_var.get()
        if os.path.isdir(p):
            try: os.startfile(p)
            except Exception as e: self.log(str(e))

    def _open_last(self):
        if self.last_output_file and os.path.isfile(self.last_output_file):
            try: os.startfile(self.last_output_file)
            except Exception as e: self.log(str(e))

    # ─── Config ──────────────────────────────────────────────────────────────
    def _load_cfg(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f: return json.load(f)
            except: pass
        return {}

    def _save_cfg(self):
        try:
            cfg = {k: getattr(self, k).get() for k in [
                'scale_var','mode_var','opacity_var','dpi_var',
                'dx_var','dy_var','angle_var','page_rot_var',
                'aa_var','aa_forza',
                'noise_var','noise_intensity','noise_max',
                'grain_var','grain_intensity','grain_max',
                'blur_var','blur_radius','blur_max',
                'contrast_var','contrast_intensity','contrast_max',
                'vignette_var','vignette_strength','vignette_max',
                'dust_var','dust_amount','dust_max',
                'warp_var','warp_amplitude','warp_max',
                'scanlines_var','scanlines_opacity','scanlines_max',
                'img_format_var','jpeg_quality_var','colore_mode_var',
                'suffisso_var','salva_in_origine_var',
            ]}
            cfg['ultima_filigrana'] = self.watermark_path
            cfg['output_dir']       = self.output_dir_var.get()
            with open(CONFIG_FILE,'w') as f: json.dump(cfg,f)
        except: pass

    def _carica_ultima_filigrana(self):
        p = self.config_data.get('ultima_filigrana')
        if p and os.path.isfile(p): self._load_img(p)

    def _load_settings(self):
        c = self.config_data
        for k in ['scale_var','mode_var','opacity_var','dpi_var',
                  'dx_var','dy_var','angle_var','page_rot_var',
                  'aa_var','aa_forza',
                  'noise_var','noise_intensity','noise_max',
                  'grain_var','grain_intensity','grain_max',
                  'blur_var','blur_radius','blur_max',
                  'contrast_var','contrast_intensity','contrast_max',
                  'vignette_var','vignette_strength','vignette_max',
                  'dust_var','dust_amount','dust_max',
                  'warp_var','warp_amplitude','warp_max',
                  'scanlines_var','scanlines_opacity','scanlines_max',
                  'img_format_var','jpeg_quality_var','colore_mode_var',
                  'suffisso_var','salva_in_origine_var']:
            if k in c:
                try: getattr(self, k).set(c[k])
                except: pass
        if 'output_dir' in c and os.path.isdir(c['output_dir']):
            self.output_dir_var.set(c['output_dir'])
        self._toggle_outdir()
        # Aggiorna range slider
        for _, (sl, mv, vv) in self._sliders.items():
            self._upd_slider(sl, mv, vv)

    # ─── Parametri ────────────────────────────────────────────────────────────
    def _get_params(self):
        return {
            'scale':             float(self.scale_var.get()),
            'max_dx':            float(self.dx_var.get()),
            'max_dy':            float(self.dy_var.get()),
            'max_angle':         float(self.angle_var.get()),
            'mode':              self.mode_var.get(),
            'opacity':           float(self.opacity_var.get()),
            'dpi':               int(self.dpi_var.get()),
            'max_page_rotation': float(self.page_rot_var.get()),
            'aa_forza':          int(self.aa_forza.get()) if self.aa_var.get() else 1,
        }

    def _get_effetti(self):
        return {
            'noise_enabled':      self.noise_var.get(),
            'noise_intensity':    self.noise_intensity.get(),
            'grain_enabled':      self.grain_var.get(),
            'grain_intensity':    self.grain_intensity.get(),
            'blur_enabled':       self.blur_var.get(),
            'blur_radius':        self.blur_radius.get(),
            'contrast_enabled':   self.contrast_var.get(),
            'contrast_intensity': self.contrast_intensity.get(),
            'vignette_enabled':   self.vignette_var.get(),
            'vignette_strength':  self.vignette_strength.get(),
            'dust_enabled':       self.dust_var.get(),
            'dust_amount':        self.dust_amount.get(),
            'warp_enabled':       self.warp_var.get(),
            'warp_amplitude':     self.warp_amplitude.get(),
            'scanlines_enabled':  self.scanlines_var.get(),
            'scanlines_opacity':  self.scanlines_opacity.get(),
        }

    # ─── Anteprima (thread) ───────────────────────────────────────────────────
    def _anteprima(self, update_only=False):
        if not self.watermark_image:
            if not update_only:
                messagebox.showwarning("Attenzione","Nessuna immagine filigrana.")
            return
        if not self.pdf_files:
            if not update_only:
                messagebox.showwarning("Attenzione","Nessun PDF.")
            return
        if update_only and (not self.preview_window or
                            not self.preview_window.winfo_exists()):
            return
        try:
            params  = self._get_params()
            effetti = self._get_effetti()
        except ValueError as e:
            if not update_only: messagebox.showerror("Errore",str(e))
            return

        fmt    = self.img_format_var.get()
        q_jpeg = self.jpeg_quality_var.get()
        colore = self.colore_mode_var.get()

        def _gen():
            try:
                wm = self.watermark_image.copy()
                if params['opacity'] < 1.0:
                    wm = modifica_opacita(wm, params['opacity'])
                tw, th = calcola_dimensioni(wm, params['mode'],
                                            params['scale'], A4_WIDTH, A4_HEIGHT)
                reader = PdfReader(self.pdf_files[0])
                if not reader.pages: return
                page = reader.pages[0]
                dx  = random.uniform(-params['max_dx'],    params['max_dx'])
                dy  = random.uniform(-params['max_dy'],    params['max_dy'])
                ang = random.uniform(-params['max_angle'], params['max_angle'])
                ov  = PdfReader(
                    io.BytesIO(genera_overlay(wm,dx,dy,ang,tw,th))
                ).pages[0]
                page.merge_page(ov)
                tmp = PdfWriter(); tmp.add_page(page)
                buf = io.BytesIO(); tmp.write(buf)
                img = rasterizza_pagina(buf.getvalue(), 96,
                                        params['max_page_rotation'],
                                        params['aa_forza'],
                                        effetti, fmt, q_jpeg, colore)
                self.after(0, lambda: self._show_prev_window(img, update_only))
            except Exception as e:
                print(f"Errore anteprima: {e}")

        threading.Thread(target=_gen, daemon=True).start()

    def _show_prev_window(self, img, update_only):
        if self.preview_window is None or not self.preview_window.winfo_exists():
            pw = Toplevel(self)
            pw.title("Anteprima  —  🖱 rotella = zoom  |  drag = scorrimento")
            pw.geometry("820x670")
            self.preview_window = pw

            # Pulsanti in basso (prima del canvas così pack li mette in fondo)
            bf = ttk.Frame(pw); bf.pack(side=BOTTOM, fill=X, padx=6, pady=4)
            self.zoom_label = StringVar(value="Zoom: 100%")
            ttk.Label(bf, textvariable=self.zoom_label,
                      foreground='gray', width=12).pack(side=RIGHT, padx=6)

            # Canvas con scrollbar — usiamo create_image direttamente,
            # NON un Frame intermedio: così scrollregion funziona sempre.
            mf = ttk.Frame(pw); mf.pack(fill=BOTH, expand=True)
            hs = ttk.Scrollbar(mf, orient=HORIZONTAL)
            vs = ttk.Scrollbar(mf, orient=VERTICAL)
            cv = Canvas(mf, bg='#404040', cursor='crosshair',
                        xscrollcommand=hs.set, yscrollcommand=vs.set)
            hs.config(command=cv.xview)
            vs.config(command=cv.yview)
            cv.grid(row=0, column=0, sticky='nsew')
            hs.grid(row=1, column=0, sticky='ew')
            vs.grid(row=0, column=1, sticky='ns')
            mf.rowconfigure(0, weight=1); mf.columnconfigure(0, weight=1)

            self._prev_canvas  = cv
            self._prev_orig    = img
            self.zoom_factor   = 1.0
            self._prev_img_id  = None   # id dell'item canvas

            def _redraw():
                nw = max(1, int(self._prev_orig.width  * self.zoom_factor))
                nh = max(1, int(self._prev_orig.height * self.zoom_factor))
                res = self._prev_orig.resize((nw, nh), Image.Resampling.LANCZOS)
                self._prev_tk = ImageTk.PhotoImage(res)
                # Aggiorna o crea l'item immagine sul canvas
                if self._prev_img_id is None:
                    self._prev_img_id = cv.create_image(0, 0, anchor='nw',
                                                        image=self._prev_tk)
                else:
                    cv.itemconfig(self._prev_img_id, image=self._prev_tk)
                # scrollregion = dimensione reale dell'immagine scalata
                cv.config(scrollregion=(0, 0, nw, nh))
                self.zoom_label.set(f"Zoom: {self.zoom_factor*100:.0f}%")

            self._redraw_prev = _redraw

            # ── Zoom con rotella ─────────────────────────────────────────────
            def _wheel(event):
                # Calcola il punto del canvas sotto il cursore
                cx = cv.canvasx(event.x)
                cy = cv.canvasy(event.y)
                old_zoom = self.zoom_factor
                factor = 1.15 if event.delta > 0 else (1 / 1.15)
                self.zoom_factor = max(0.05, min(8.0, old_zoom * factor))
                _redraw()
                # Riscala la vista in modo che il punto sotto il cursore resti fermo
                ratio = self.zoom_factor / old_zoom
                new_cx = cx * ratio
                new_cy = cy * ratio
                # Converti in frazione per xview_moveto
                tot_w = self._prev_orig.width  * self.zoom_factor
                tot_h = self._prev_orig.height * self.zoom_factor
                win_w = cv.winfo_width()
                win_h = cv.winfo_height()
                frac_x = max(0.0, (new_cx - event.x) / tot_w)
                frac_y = max(0.0, (new_cy - event.y) / tot_h)
                cv.xview_moveto(frac_x)
                cv.yview_moveto(frac_y)

            cv.bind('<MouseWheel>', _wheel)           # Windows
            cv.bind('<Button-4>',                     # Linux scroll su
                    lambda e: [setattr(self, 'zoom_factor',
                        min(8.0, self.zoom_factor * 1.15)), _redraw()])
            cv.bind('<Button-5>',                     # Linux scroll giù
                    lambda e: [setattr(self, 'zoom_factor',
                        max(0.05, self.zoom_factor / 1.15)), _redraw()])

            # ── Drag per scorrere ────────────────────────────────────────────
            self._drag_start = None
            def _drag_start(e):
                cv.config(cursor='fleur')
                self._drag_start = (e.x, e.y)
            def _drag_move(e):
                if self._drag_start:
                    dx = self._drag_start[0] - e.x
                    dy = self._drag_start[1] - e.y
                    self._drag_start = (e.x, e.y)
                    cv.xview_scroll(dx, 'units')
                    cv.yview_scroll(dy, 'units')
            def _drag_end(e):
                cv.config(cursor='crosshair')
                self._drag_start = None
            cv.bind('<ButtonPress-1>',  _drag_start)
            cv.bind('<B1-Motion>',      _drag_move)
            cv.bind('<ButtonRelease-1>',_drag_end)

            # Pulsanti zoom
            ttk.Button(bf, text="Zoom +",
                command=lambda: [setattr(self,'zoom_factor',
                    min(8.0,self.zoom_factor*1.3)), _redraw()]).pack(side=LEFT,padx=2)
            ttk.Button(bf, text="Zoom −",
                command=lambda: [setattr(self,'zoom_factor',
                    max(0.05,self.zoom_factor/1.3)), _redraw()]).pack(side=LEFT,padx=2)
            ttk.Button(bf, text="1:1",
                command=lambda: [setattr(self,'zoom_factor',1.0), _redraw()]).pack(side=LEFT,padx=2)

            def _fit():
                pw.update_idletasks()
                fw = cv.winfo_width()  - 4
                fh = cv.winfo_height() - 4
                if fw > 0 and fh > 0:
                    zx = fw / self._prev_orig.width
                    zy = fh / self._prev_orig.height
                    self.zoom_factor = min(zx, zy)
                    _redraw()
            ttk.Button(bf, text="Adatta", command=_fit).pack(side=LEFT, padx=2)
            ttk.Label(bf, text="  🖱 rotella=zoom  |  drag=sposta",
                      foreground='gray', font=('TkDefaultFont',8)).pack(side=LEFT,padx=8)

            _redraw()
        else:
            self._prev_orig = img
            self._prev_img_id = None   # forza ricreazione item canvas
            self._redraw_prev()

        self.log("Anteprima aggiornata.")

    # ─── Elaborazione ─────────────────────────────────────────────────────────
    def _avvia(self):
        if self.processing: return
        if not self.pdf_files:
            messagebox.showwarning("Attenzione","Nessun PDF."); return
        if not self.watermark_image:
            messagebox.showwarning("Attenzione","Nessuna immagine."); return

        try:
            params = self._get_params()
        except ValueError as e:
            messagebox.showerror("Errore",str(e)); return

        out_dir = self.output_dir_var.get()
        if not self.salva_in_origine_var.get():
            os.makedirs(out_dir, exist_ok=True)

        effetti  = self._get_effetti()
        fmt      = self.img_format_var.get()
        q_jpeg   = self.jpeg_quality_var.get()
        colore   = self.colore_mode_var.get()
        suffisso = self.suffisso_var.get().strip() or '_watermarked'

        self._save_cfg()
        self.processing   = True
        self._results_accum = []
        self.btn_avvia.config(state=DISABLED)
        self.btn_stop.config(state=NORMAL)

        # Conta totale pagine per progresso granulare
        self._total_pages = 0
        self._readers_cache = {}
        for p in self.pdf_files:
            try:
                r = PdfReader(p)
                n = len(r.pages)
                self._readers_cache[p] = n
                self._total_pages += n
            except:
                self._readers_cache[p] = 1
                self._total_pages += 1

        self.progress['maximum'] = self._total_pages
        self.progress['value']   = 0
        self._pages_done         = 0
        self.prog_label.set(f"0 / {self._total_pages} pag.")

        # Serializza watermark
        wm_buf = io.BytesIO()
        self.watermark_image.save(wm_buf, format='PNG')
        wm_bytes = wm_buf.getvalue()
        wm_mode  = self.watermark_image.mode

        # Manager per dict condivisa progresso inter-processo
        # Usiamo invece una Queue passata come None e monitoriamo
        # i risultati via apply_async callback per task completati.
        # Per il progresso per-pagina usiamo un thread monitor.

        tasks = []
        for i, pdf_path in enumerate(self.pdf_files):
            od = (os.path.dirname(pdf_path)
                  if self.salva_in_origine_var.get() else out_dir)
            tasks.append((pdf_path, wm_bytes, wm_mode, od,
                          params, effetti, fmt, q_jpeg, colore,
                          suffisso, None, i))

        pool = self._get_pool()
        self._pending = len(tasks)

        for task in tasks:
            pool.apply_async(
                _worker, (task,),
                callback=self._on_task_done,
                error_callback=self._on_task_err
            )

        self.log(f"Avviati {len(tasks)} PDF...")

    def _on_task_done(self, res):
        """Chiamato nel thread principale quando UN pdf è finito."""
        pdf_path, success, out_or_err, n_pag = res
        self._results_accum.append(res)
        if success:
            self.last_output_file = out_or_err
            self.after(0, lambda p=pdf_path, o=out_or_err:
                self.log(f"✓ {os.path.basename(p)} → {os.path.basename(o)}"))
        else:
            self.after(0, lambda p=pdf_path, e=out_or_err:
                self.log(f"✗ {os.path.basename(p)}: {e[:80]}"))

        # Aggiorna progresso: aggiungiamo le pagine di questo file
        n = self._readers_cache.get(pdf_path, 1)
        self._pages_done += n
        self.after(0, self._update_progress)

        self._pending -= 1
        if self._pending <= 0:
            self.after(0, self._done)

    def _on_task_err(self, exc):
        self.after(0, lambda: self.log(f"Errore pool: {exc}"))
        self._pending -= 1
        if self._pending <= 0:
            self.after(0, self._done)

    def _update_progress(self):
        done = min(self._pages_done, self._total_pages)
        self.progress['value'] = done
        pct = int(done / max(1, self._total_pages) * 100)
        self.prog_label.set(f"{done} / {self._total_pages} pag. ({pct}%)")

    def _done(self):
        self.processing = False
        self.btn_avvia.config(state=NORMAL)
        self.btn_stop.config(state=DISABLED)
        self.progress['value'] = self._total_pages
        self.prog_label.set(f"✅ {self._total_pages} / {self._total_pages} pag. (100%)")
        self.log("✅ Completato.")
        # Mostra dialogo con file cliccabili
        DiaologoCompletato(self, self._results_accum)

    def _stop(self):
        if self.processing:
            if self._mp_pool:
                self._mp_pool.terminate(); self._mp_pool.join()
                self._mp_pool = None
            self.processing = False
            self.btn_avvia.config(state=NORMAL)
            self.btn_stop.config(state=DISABLED)
            self.log("⏹ Interrotto.")

    # ─── Tick ────────────────────────────────────────────────────────────────
    def _tick(self):
        self.after(150, self._tick)

    def log(self, msg):
        self.log_text.insert(END, msg+"\n")
        self.log_text.see(END)


# ── Helper globale (non metodo, usato nel _build_ui) ─────────────────────────
def _lbl(parent, text):
    ttk.Label(parent, text=text).pack(side=LEFT, padx=(4,0))


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = PdfSporcaApp()
    app.mainloop()
