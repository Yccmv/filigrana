#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Applicazione grafica per applicare una filigrana "sporca" a PDF multipli,
rasterizzare con rotazioni casuali, applicare effetti da scansione
e salvare il risultato. Supporta drag & drop e salvataggio configurazione.
"""

import os
import sys
import json
import threading
import queue
import random
import io
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from tkinterdnd2 import DND_FILES, TkinterDnD

from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageTk, ImageFilter, ImageEnhance, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
import fitz  # PyMuPDF

# Costanti
CONFIG_FILE = os.path.join(os.path.dirname(sys.argv[0]), 'config.json')
A4_WIDTH, A4_HEIGHT = A4
MM_TO_PT = 72.0 / 25.4

# ------------------------------------------------------------
# Funzioni di elaborazione (con effetti)
# ------------------------------------------------------------
def modifica_opacita(immagine_pil, opacita):
    if opacita >= 1.0:
        return immagine_pil
    if immagine_pil.mode != 'RGBA':
        immagine_pil = immagine_pil.convert('RGBA')
    r, g, b, a = immagine_pil.split()
    a = a.point(lambda i: int(i * opacita))
    return Image.merge('RGBA', (r, g, b, a))

def calcola_dimensioni_immagine(immagine_pil, mode, scale, page_width, page_height):
    img_width, img_height = immagine_pil.size
    ratio_img = img_width / img_height
    ratio_page = page_width / page_height

    if mode == 'cover':
        if ratio_img > ratio_page:
            height_pt = page_height
            width_pt = height_pt * ratio_img
        else:
            width_pt = page_width
            height_pt = width_pt / ratio_img
    else:
        if ratio_img > ratio_page:
            width_pt = page_width
            height_pt = width_pt / ratio_img
        else:
            height_pt = page_height
            width_pt = height_pt * ratio_img

    width_pt *= scale
    height_pt *= scale
    return width_pt, height_pt

def genera_overlay(immagine_pil, dx_mm, dy_mm, angle_deg, width_pt, height_pt):
    dx_pt = dx_mm * MM_TO_PT
    dy_pt = dy_mm * MM_TO_PT
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    center_x, center_y = A4_WIDTH / 2, A4_HEIGHT / 2
    c.saveState()
    c.translate(center_x + dx_pt, center_y + dy_pt)
    c.rotate(angle_deg)
    c.translate(-width_pt / 2, -height_pt / 2)
    c.drawImage(ImageReader(immagine_pil), 0, 0, width=width_pt, height=height_pt, mask='auto')
    c.restoreState()
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.read()

def applica_effetti(immagine, params_effetti):
    """
    Applica una serie di effetti all'immagine PIL per simulare una scansione imperfetta.
    params_effetti: dizionario con chiavi booleane e intensità.
    """
    img = immagine.copy().convert('RGB')  # lavoriamo in RGB

    # 1. Rumore gaussiano
    if params_effetti.get('noise_enabled', False):
        intensita = params_effetti.get('noise_intensity', 0.1)  # 0-1
        import numpy as np
        arr = np.array(img).astype(np.float32)
        noise = np.random.normal(0, intensita * 255, arr.shape)
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)

    # 2. Sfocatura leggera
    if params_effetti.get('blur_enabled', False):
        raggio = params_effetti.get('blur_radius', 1.0)
        img = img.filter(ImageFilter.GaussianBlur(radius=raggio))

    # 3. Variazione casuale di contrasto/luminosità
    if params_effetti.get('contrast_enabled', False):
        intensita = params_effetti.get('contrast_intensity', 0.2)
        factor_contrast = 1.0 + random.uniform(-intensita, intensita)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(factor_contrast)
        factor_brightness = 1.0 + random.uniform(-intensita/2, intensita/2)
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(factor_brightness)

    # 4. Linee di scansione orizzontali
    if params_effetti.get('scanlines_enabled', False):
        opacita = params_effetti.get('scanlines_opacity', 0.1)
        img = img.convert('RGBA')
        pixels = img.load()
        w, h = img.size
        for y in range(0, h, 4):
            for x in range(w):
                r, g, b, a = pixels[x, y]
                r = int(r * (1 - opacita))
                g = int(g * (1 - opacita))
                b = int(b * (1 - opacita))
                pixels[x, y] = (r, g, b, a)
        img = img.convert('RGB')

    return img

def rasterizza_con_rotazione(pdf_bytes, dpi, max_rotazione, effetti_params, formato_img='PNG', qualita_jpeg=95):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Applica effetti
    img = applica_effetti(img, effetti_params)

    # Rotazione finale
    if max_rotazione > 0:
        angolo = random.uniform(-max_rotazione, max_rotazione)
        img = img.rotate(angolo, expand=True, fillcolor=(255, 255, 255))

    # Se richiesto, converti in JPEG per ridurre peso
    if formato_img == 'JPEG':
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=qualita_jpeg, optimize=True)
        buf.seek(0)
        img = Image.open(buf)  # ora è basata sul JPEG (ma sempre PIL)
    return img

def processa_pdf(pdf_path, watermark_img, output_dir, params, effetti_params, formato_img, qualita_jpeg, q_progress, q_result):
    """
    Elabora un singolo PDF.
    params: dizionario con scale, max_dx, max_dy, max_angle, mode, opacity, dpi, max_page_rotation
    effetti_params: dizionario con impostazioni effetti
    """
    try:
        # Prepara immagine filigrana
        img_water = watermark_img.copy()
        if params['opacity'] < 1.0:
            img_water = modifica_opacita(img_water, params['opacity'])

        target_width, target_height = calcola_dimensioni_immagine(
            img_water, params['mode'], params['scale'],
            A4_WIDTH, A4_HEIGHT
        )

        reader = PdfReader(pdf_path)
        num_pag = len(reader.pages)

        base = os.path.basename(pdf_path)
        name, ext = os.path.splitext(base)
        out_filename = f"{name}_watermarked.pdf"
        out_path = os.path.join(output_dir, out_filename)

        c = canvas.Canvas(out_path, pagesize=A4)

        for i, page in enumerate(reader.pages, start=1):
            dx = random.uniform(-params['max_dx'], params['max_dx'])
            dy = random.uniform(-params['max_dy'], params['max_dy'])
            ang_filigrana = random.uniform(-params['max_angle'], params['max_angle'])

            overlay_pdf = genera_overlay(
                img_water, dx, dy, ang_filigrana,
                target_width, target_height
            )

            overlay_reader = PdfReader(io.BytesIO(overlay_pdf))
            overlay_page = overlay_reader.pages[0]
            page.merge_page(overlay_page)

            temp_writer = PdfWriter()
            temp_writer.add_page(page)
            temp_pdf = io.BytesIO()
            temp_writer.write(temp_pdf)
            temp_pdf.seek(0)
            pagina_pdf_bytes = temp_pdf.read()

            img_pagina = rasterizza_con_rotazione(
                pagina_pdf_bytes,
                dpi=params['dpi'],
                max_rotazione=params['max_page_rotation'],
                effetti_params=effetti_params,
                formato_img=formato_img,
                qualita_jpeg=qualita_jpeg
            )

            # Aggiungi immagine al PDF finale (con eventuale compressione)
            c.setPageSize((A4_WIDTH, A4_HEIGHT))
            img_w, img_h = img_pagina.size
            scale = min(A4_WIDTH / img_w, A4_HEIGHT / img_h)
            new_w = img_w * scale
            new_h = img_h * scale
            x = (A4_WIDTH - new_w) / 2
            y = (A4_HEIGHT - new_h) / 2

            # Per JPEG, passiamo un buffer per evitare doppia compressione
            if formato_img == 'JPEG' and qualita_jpeg < 100:
                buf = io.BytesIO()
                img_pagina.save(buf, format='JPEG', quality=qualita_jpeg, optimize=True)
                buf.seek(0)
                c.drawImage(ImageReader(buf), x, y, width=new_w, height=new_h, mask='auto')
            else:
                c.drawImage(ImageReader(img_pagina), x, y, width=new_w, height=new_h, mask='auto')

            c.showPage()

            q_progress.put((pdf_path, i, num_pag))

        c.save()
        q_result.put((pdf_path, True, out_path))
    except Exception as e:
        q_result.put((pdf_path, False, str(e)))

# ------------------------------------------------------------
# Classe principale dell'applicazione
# ------------------------------------------------------------
class PdfSporcaApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Sporca - Filigrana, effetti scansione e rasterizzazione")
        self.geometry("1100x850")
        self.resizable(True, True)

        # Variabili di stato
        self.pdf_files = []
        self.watermark_path = None
        self.watermark_image = None
        self.thumbnail = None
        self.config = self.carica_config()
        self.queue_progress = queue.Queue()
        self.queue_result = queue.Queue()
        self.processing = False
        self.current_pdf_index = 0
        self.total_pdfs = 0

        # Crea l'interfaccia
        self.crea_widgets()
        self.aggiorna_lista_pdf()
        self.carica_ultima_filigrana()
        self.carica_impostazioni()
        self.after(100, self.processa_code)

        # Abilita drop su tutta la finestra?
        self.drop_target_register(DND_FILES)
        self.dnd_bind('<<Drop>>', self.on_drop)

    def crea_widgets(self):
        # Notebook per organizzare meglio
        notebook = ttk.Notebook(self)
        notebook.pack(fill=BOTH, expand=True, padx=5, pady=5)

        # Tab principale
        main_tab = ttk.Frame(notebook)
        notebook.add(main_tab, text="Principale")

        # Tab effetti
        effetti_tab = ttk.Frame(notebook)
        notebook.add(effetti_tab, text="Effetti scansione")

        # ----- Tab principale -----
        main_frame = ttk.Frame(main_tab, padding="10")
        main_frame.pack(fill=BOTH, expand=True)

        # Area PDF e immagine (sinistra/destra)
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill=BOTH, expand=True, pady=5)

        # Lista PDF
        pdf_frame = ttk.LabelFrame(top_frame, text="PDF da elaborare (drag & drop)", padding=5)
        pdf_frame.pack(side=LEFT, fill=BOTH, expand=True, padx=5)

        self.pdf_listbox = Listbox(pdf_frame, selectmode=EXTENDED, height=10)
        self.pdf_listbox.pack(fill=BOTH, expand=True, side=LEFT)

        pdf_scroll = ttk.Scrollbar(pdf_frame, orient=VERTICAL, command=self.pdf_listbox.yview)
        pdf_scroll.pack(side=RIGHT, fill=Y)
        self.pdf_listbox.config(yscrollcommand=pdf_scroll.set)

        pdf_btn_frame = ttk.Frame(pdf_frame)
        pdf_btn_frame.pack(fill=X, pady=5)
        ttk.Button(pdf_btn_frame, text="Aggiungi PDF", command=self.aggiungi_pdf_dialog).pack(side=LEFT, padx=2)
        ttk.Button(pdf_btn_frame, text="Rimuovi selezionati", command=self.rimuovi_pdf).pack(side=LEFT, padx=2)
        ttk.Button(pdf_btn_frame, text="Svuota lista", command=self.svuota_pdf).pack(side=LEFT, padx=2)

        self.pdf_listbox.drop_target_register(DND_FILES)
        self.pdf_listbox.dnd_bind('<<Drop>>', self.on_drop_pdf)

        # Area immagine
        img_frame = ttk.LabelFrame(top_frame, text="Immagine filigrana (drag & drop)", padding=5)
        img_frame.pack(side=RIGHT, fill=BOTH, expand=True, padx=5)

        self.preview_label = Label(img_frame, bg='gray', relief=SUNKEN, width=30, height=15)
        self.preview_label.pack(pady=5, padx=5, fill=BOTH, expand=True)

        self.img_info = StringVar(value="Nessuna immagine")
        ttk.Label(img_frame, textvariable=self.img_info).pack(pady=2)

        img_btn_frame = ttk.Frame(img_frame)
        img_btn_frame.pack(fill=X, pady=5)
        ttk.Button(img_btn_frame, text="Scegli immagine...", command=self.scegli_immagine).pack(side=LEFT, padx=2)
        ttk.Button(img_btn_frame, text="Rimuovi", command=self.rimuovi_immagine).pack(side=LEFT, padx=2)

        self.preview_label.drop_target_register(DND_FILES)
        self.preview_label.dnd_bind('<<Drop>>', self.on_drop_img)

        # Parametri di elaborazione
        param_frame = ttk.LabelFrame(main_frame, text="Parametri filigrana", padding=5)
        param_frame.pack(fill=X, pady=5)

        row1 = ttk.Frame(param_frame)
        row1.pack(fill=X, pady=2)
        ttk.Label(row1, text="Scala:").pack(side=LEFT)
        self.scale_var = DoubleVar(value=1.0)
        ttk.Entry(row1, textvariable=self.scale_var, width=8).pack(side=LEFT, padx=5)

        ttk.Label(row1, text="Modalità:").pack(side=LEFT, padx=(10,0))
        self.mode_var = StringVar(value='cover')
        ttk.Combobox(row1, textvariable=self.mode_var, values=['cover', 'contain'], width=8, state='readonly').pack(side=LEFT, padx=5)

        ttk.Label(row1, text="Opacità:").pack(side=LEFT, padx=(10,0))
        self.opacity_var = DoubleVar(value=1.0)
        ttk.Scale(row1, from_=0.0, to=1.0, orient=HORIZONTAL, variable=self.opacity_var, length=100).pack(side=LEFT, padx=5)
        ttk.Label(row1, textvariable=self.opacity_var, width=4).pack(side=LEFT)

        ttk.Label(row1, text="DPI:").pack(side=LEFT, padx=(10,0))
        self.dpi_var = IntVar(value=150)
        ttk.Entry(row1, textvariable=self.dpi_var, width=6).pack(side=LEFT, padx=5)

        row2 = ttk.Frame(param_frame)
        row2.pack(fill=X, pady=2)
        ttk.Label(row2, text="Max dx (mm):").pack(side=LEFT)
        self.dx_var = DoubleVar(value=0.0)
        ttk.Entry(row2, textvariable=self.dx_var, width=6).pack(side=LEFT, padx=5)

        ttk.Label(row2, text="Max dy (mm):").pack(side=LEFT, padx=(10,0))
        self.dy_var = DoubleVar(value=0.0)
        ttk.Entry(row2, textvariable=self.dy_var, width=6).pack(side=LEFT, padx=5)

        ttk.Label(row2, text="Max angolo filigrana (°):").pack(side=LEFT, padx=(10,0))
        self.angle_var = DoubleVar(value=0.0)
        ttk.Entry(row2, textvariable=self.angle_var, width=6).pack(side=LEFT, padx=5)

        ttk.Label(row2, text="Max rotazione pagina (°):").pack(side=LEFT, padx=(10,0))
        self.page_rot_var = DoubleVar(value=1.0)
        ttk.Entry(row2, textvariable=self.page_rot_var, width=6).pack(side=LEFT, padx=5)

        row3 = ttk.Frame(param_frame)
        row3.pack(fill=X, pady=5)
        ttk.Label(row3, text="Cartella output:").pack(side=LEFT)
        self.output_dir_var = StringVar(value=os.getcwd())
        ttk.Entry(row3, textvariable=self.output_dir_var, width=50).pack(side=LEFT, padx=5, fill=X, expand=True)
        ttk.Button(row3, text="Sfoglia...", command=self.scegli_output_dir).pack(side=LEFT, padx=2)

        # ----- Tab effetti con controllo dei valori massimi -----
        effetti_frame = ttk.Frame(effetti_tab, padding="10")
        effetti_frame.pack(fill=BOTH, expand=True)

        # Variabili per gli effetti (inclusi i max)
        self.noise_var = BooleanVar(value=False)
        self.noise_intensity = DoubleVar(value=0.1)
        self.noise_max = DoubleVar(value=0.5)  # default max

        self.blur_var = BooleanVar(value=False)
        self.blur_radius = DoubleVar(value=1.0)
        self.blur_max = DoubleVar(value=3.0)

        self.contrast_var = BooleanVar(value=False)
        self.contrast_intensity = DoubleVar(value=0.2)
        self.contrast_max = DoubleVar(value=0.5)

        self.scanlines_var = BooleanVar(value=False)
        self.scanlines_opacity = DoubleVar(value=0.1)
        self.scanlines_max = DoubleVar(value=0.3)

        # Riga 1: rumore
        f1 = ttk.Frame(effetti_frame)
        f1.pack(fill=X, pady=5)
        ttk.Checkbutton(f1, text="Rumore gaussiano", variable=self.noise_var).pack(side=LEFT)
        ttk.Label(f1, text="Intensità:").pack(side=LEFT, padx=(20,5))
        self.noise_slider = ttk.Scale(f1, from_=0.0, to=self.noise_max.get(), orient=HORIZONTAL,
                                      variable=self.noise_intensity, length=150)
        self.noise_slider.pack(side=LEFT, padx=5)
        self.noise_entry = ttk.Entry(f1, textvariable=self.noise_intensity, width=6)
        self.noise_entry.pack(side=LEFT, padx=2)
        ttk.Label(f1, text="Max:").pack(side=LEFT, padx=(10,2))
        self.noise_max_entry = ttk.Entry(f1, textvariable=self.noise_max, width=5)
        self.noise_max_entry.pack(side=LEFT)
        self.noise_max_entry.bind('<Return>', lambda e: self.aggiorna_slider_max('noise'))

        # Riga 2: sfocatura
        f2 = ttk.Frame(effetti_frame)
        f2.pack(fill=X, pady=5)
        ttk.Checkbutton(f2, text="Sfocatura", variable=self.blur_var).pack(side=LEFT)
        ttk.Label(f2, text="Raggio:").pack(side=LEFT, padx=(20,5))
        self.blur_slider = ttk.Scale(f2, from_=0.0, to=self.blur_max.get(), orient=HORIZONTAL,
                                     variable=self.blur_radius, length=150)
        self.blur_slider.pack(side=LEFT, padx=5)
        self.blur_entry = ttk.Entry(f2, textvariable=self.blur_radius, width=6)
        self.blur_entry.pack(side=LEFT, padx=2)
        ttk.Label(f2, text="Max:").pack(side=LEFT, padx=(10,2))
        self.blur_max_entry = ttk.Entry(f2, textvariable=self.blur_max, width=5)
        self.blur_max_entry.pack(side=LEFT)
        self.blur_max_entry.bind('<Return>', lambda e: self.aggiorna_slider_max('blur'))

        # Riga 3: contrasto
        f3 = ttk.Frame(effetti_frame)
        f3.pack(fill=X, pady=5)
        ttk.Checkbutton(f3, text="Variazione contrasto/luminosità", variable=self.contrast_var).pack(side=LEFT)
        ttk.Label(f3, text="Intensità:").pack(side=LEFT, padx=(20,5))
        self.contrast_slider = ttk.Scale(f3, from_=0.0, to=self.contrast_max.get(), orient=HORIZONTAL,
                                         variable=self.contrast_intensity, length=150)
        self.contrast_slider.pack(side=LEFT, padx=5)
        self.contrast_entry = ttk.Entry(f3, textvariable=self.contrast_intensity, width=6)
        self.contrast_entry.pack(side=LEFT, padx=2)
        ttk.Label(f3, text="Max:").pack(side=LEFT, padx=(10,2))
        self.contrast_max_entry = ttk.Entry(f3, textvariable=self.contrast_max, width=5)
        self.contrast_max_entry.pack(side=LEFT)
        self.contrast_max_entry.bind('<Return>', lambda e: self.aggiorna_slider_max('contrast'))

        # Riga 4: linee di scansione
        f4 = ttk.Frame(effetti_frame)
        f4.pack(fill=X, pady=5)
        ttk.Checkbutton(f4, text="Linee di scansione", variable=self.scanlines_var).pack(side=LEFT)
        ttk.Label(f4, text="Opacità:").pack(side=LEFT, padx=(20,5))
        self.scanlines_slider = ttk.Scale(f4, from_=0.0, to=self.scanlines_max.get(), orient=HORIZONTAL,
                                          variable=self.scanlines_opacity, length=150)
        self.scanlines_slider.pack(side=LEFT, padx=5)
        self.scanlines_entry = ttk.Entry(f4, textvariable=self.scanlines_opacity, width=6)
        self.scanlines_entry.pack(side=LEFT, padx=2)
        ttk.Label(f4, text="Max:").pack(side=LEFT, padx=(10,2))
        self.scanlines_max_entry = ttk.Entry(f4, textvariable=self.scanlines_max, width=5)
        self.scanlines_max_entry.pack(side=LEFT)
        self.scanlines_max_entry.bind('<Return>', lambda e: self.aggiorna_slider_max('scanlines'))

        # ----- Opzioni di compressione (riduzione peso) -----
        compress_frame = ttk.LabelFrame(effetti_frame, text="Compressione immagini (riduci peso PDF)", padding=5)
        compress_frame.pack(fill=X, pady=15)

        row_comp = ttk.Frame(compress_frame)
        row_comp.pack(fill=X, pady=2)
        ttk.Label(row_comp, text="Formato:").pack(side=LEFT)
        self.img_format_var = StringVar(value='PNG')
        ttk.Combobox(row_comp, textvariable=self.img_format_var, values=['PNG', 'JPEG'], width=8, state='readonly').pack(side=LEFT, padx=5)

        ttk.Label(row_comp, text="Qualità JPEG:").pack(side=LEFT, padx=(20,5))
        self.jpeg_quality_var = IntVar(value=85)
        self.jpeg_slider = ttk.Scale(row_comp, from_=1, to=100, orient=HORIZONTAL, variable=self.jpeg_quality_var, length=150)
        self.jpeg_slider.pack(side=LEFT, padx=5)
        self.jpeg_entry = ttk.Entry(row_comp, textvariable=self.jpeg_quality_var, width=5)
        self.jpeg_entry.pack(side=LEFT)

        # Nota informativa
        ttk.Label(compress_frame, text="JPEG riduce il peso ma può introdurre leggeri artefatti. Con PNG il file sarà più grande ma senza perdite.",
                  foreground="gray").pack(anchor=W, pady=2)

        # ----- Barra di progresso e pulsanti (fuori dal notebook) -----
        control_frame = ttk.Frame(self)
        control_frame.pack(fill=X, padx=10, pady=5)

        self.progress = ttk.Progressbar(control_frame, orient=HORIZONTAL, length=400, mode='determinate')
        self.progress.pack(side=LEFT, padx=5, fill=X, expand=True)

        self.btn_avvia = ttk.Button(control_frame, text="Avvia elaborazione", command=self.avvia_elaborazione)
        self.btn_avvia.pack(side=LEFT, padx=5)

        self.btn_interrompi = ttk.Button(control_frame, text="Interrompi", command=self.interrompi_elaborazione, state=DISABLED)
        self.btn_interrompi.pack(side=LEFT, padx=5)

        # Log area
        log_frame = ttk.LabelFrame(self, text="Log", padding=5)
        log_frame.pack(fill=BOTH, expand=True, padx=10, pady=5)

        self.log_text = Text(log_frame, height=6, wrap=WORD)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient=VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=RIGHT, fill=Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

    def aggiorna_slider_max(self, effetto):
        """Aggiorna il range dello slider quando cambia il valore massimo."""
        try:
            if effetto == 'noise':
                self.noise_slider.config(to=self.noise_max.get())
                if self.noise_intensity.get() > self.noise_max.get():
                    self.noise_intensity.set(self.noise_max.get())
            elif effetto == 'blur':
                self.blur_slider.config(to=self.blur_max.get())
                if self.blur_radius.get() > self.blur_max.get():
                    self.blur_radius.set(self.blur_max.get())
            elif effetto == 'contrast':
                self.contrast_slider.config(to=self.contrast_max.get())
                if self.contrast_intensity.get() > self.contrast_max.get():
                    self.contrast_intensity.set(self.contrast_max.get())
            elif effetto == 'scanlines':
                self.scanlines_slider.config(to=self.scanlines_max.get())
                if self.scanlines_opacity.get() > self.scanlines_max.get():
                    self.scanlines_opacity.set(self.scanlines_max.get())
        except:
            pass

    # --------------------------------------------------------
    # Gestione configurazione (estesa)
    # --------------------------------------------------------
    def carica_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def salva_config(self):
        config = {
            'ultima_filigrana': self.watermark_path,
            'scale': self.scale_var.get(),
            'mode': self.mode_var.get(),
            'opacity': self.opacity_var.get(),
            'dpi': self.dpi_var.get(),
            'max_dx': self.dx_var.get(),
            'max_dy': self.dy_var.get(),
            'max_angle': self.angle_var.get(),
            'max_page_rotation': self.page_rot_var.get(),
            'output_dir': self.output_dir_var.get(),
            # Effetti e loro massimi
            'noise_enabled': self.noise_var.get(),
            'noise_intensity': self.noise_intensity.get(),
            'noise_max': self.noise_max.get(),
            'blur_enabled': self.blur_var.get(),
            'blur_radius': self.blur_radius.get(),
            'blur_max': self.blur_max.get(),
            'contrast_enabled': self.contrast_var.get(),
            'contrast_intensity': self.contrast_intensity.get(),
            'contrast_max': self.contrast_max.get(),
            'scanlines_enabled': self.scanlines_var.get(),
            'scanlines_opacity': self.scanlines_opacity.get(),
            'scanlines_max': self.scanlines_max.get(),
            # Compressione
            'img_format': self.img_format_var.get(),
            'jpeg_quality': self.jpeg_quality_var.get(),
        }
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f)
        except:
            pass

    def carica_impostazioni(self):
        """Carica tutti i parametri dalla configurazione (eccetto l'immagine)."""
        cfg = self.config
        if 'scale' in cfg: self.scale_var.set(cfg['scale'])
        if 'mode' in cfg: self.mode_var.set(cfg['mode'])
        if 'opacity' in cfg: self.opacity_var.set(cfg['opacity'])
        if 'dpi' in cfg: self.dpi_var.set(cfg['dpi'])
        if 'max_dx' in cfg: self.dx_var.set(cfg['max_dx'])
        if 'max_dy' in cfg: self.dy_var.set(cfg['max_dy'])
        if 'max_angle' in cfg: self.angle_var.set(cfg['max_angle'])
        if 'max_page_rotation' in cfg: self.page_rot_var.set(cfg['max_page_rotation'])
        if 'output_dir' in cfg and os.path.isdir(cfg['output_dir']):
            self.output_dir_var.set(cfg['output_dir'])

        # Effetti e massimi
        if 'noise_enabled' in cfg: self.noise_var.set(cfg['noise_enabled'])
        if 'noise_intensity' in cfg: self.noise_intensity.set(cfg['noise_intensity'])
        if 'noise_max' in cfg:
            self.noise_max.set(cfg['noise_max'])
            self.aggiorna_slider_max('noise')
        if 'blur_enabled' in cfg: self.blur_var.set(cfg['blur_enabled'])
        if 'blur_radius' in cfg: self.blur_radius.set(cfg['blur_radius'])
        if 'blur_max' in cfg:
            self.blur_max.set(cfg['blur_max'])
            self.aggiorna_slider_max('blur')
        if 'contrast_enabled' in cfg: self.contrast_var.set(cfg['contrast_enabled'])
        if 'contrast_intensity' in cfg: self.contrast_intensity.set(cfg['contrast_intensity'])
        if 'contrast_max' in cfg:
            self.contrast_max.set(cfg['contrast_max'])
            self.aggiorna_slider_max('contrast')
        if 'scanlines_enabled' in cfg: self.scanlines_var.set(cfg['scanlines_enabled'])
        if 'scanlines_opacity' in cfg: self.scanlines_opacity.set(cfg['scanlines_opacity'])
        if 'scanlines_max' in cfg:
            self.scanlines_max.set(cfg['scanlines_max'])
            self.aggiorna_slider_max('scanlines')

        # Compressione
        if 'img_format' in cfg: self.img_format_var.set(cfg['img_format'])
        if 'jpeg_quality' in cfg: self.jpeg_quality_var.set(cfg['jpeg_quality'])

    def carica_ultima_filigrana(self):
        path = self.config.get('ultima_filigrana')
        if path and os.path.isfile(path):
            self.carica_immagine(path)

    # --------------------------------------------------------
    # Metodi per PDF e immagine
    # --------------------------------------------------------
    def aggiorna_lista_pdf(self):
        self.pdf_listbox.delete(0, END)
        for f in self.pdf_files:
            self.pdf_listbox.insert(END, os.path.basename(f))

    def aggiungi_pdf_dialog(self):
        files = filedialog.askopenfilenames(
            title="Seleziona file PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if files:
            for f in files:
                if f not in self.pdf_files:
                    self.pdf_files.append(f)
            self.aggiorna_lista_pdf()
            self.log(f"Aggiunti {len(files)} PDF.")

    def rimuovi_pdf(self):
        sel = self.pdf_listbox.curselection()
        if sel:
            for i in reversed(sel):
                del self.pdf_files[i]
            self.aggiorna_lista_pdf()
            self.log("PDF rimossi.")

    def svuota_pdf(self):
        self.pdf_files.clear()
        self.aggiorna_lista_pdf()
        self.log("Lista PDF svuotata.")

    def scegli_immagine(self):
        path = filedialog.askopenfilename(
            title="Seleziona immagine filigrana (PNG con trasparenza)",
            filetypes=[("PNG files", "*.png"), ("All files", "*.*")]
        )
        if path:
            self.carica_immagine(path)

    def carica_immagine(self, path):
        try:
            img = Image.open(path)
            self.watermark_image = img.copy()
            self.watermark_path = path
            self.img_info.set(f"{os.path.basename(path)} ({img.width}x{img.height})")
            img.thumbnail((200, 200))
            self.thumbnail = ImageTk.PhotoImage(img)
            self.preview_label.config(image=self.thumbnail)
            self.salva_config()
            self.log(f"Immagine caricata: {path}")
        except Exception as e:
            messagebox.showerror("Errore", f"Impossibile caricare l'immagine:\n{e}")

    def rimuovi_immagine(self):
        self.watermark_image = None
        self.watermark_path = None
        self.thumbnail = None
        self.preview_label.config(image='')
        self.img_info.set("Nessuna immagine")
        self.salva_config()
        self.log("Immagine rimossa.")

    # --------------------------------------------------------
    # Drag & drop
    # --------------------------------------------------------
    def on_drop_pdf(self, event):
        files = self.parse_drop_files(event.data)
        for f in files:
            if f.lower().endswith('.pdf') and f not in self.pdf_files:
                self.pdf_files.append(f)
        self.aggiorna_lista_pdf()
        self.log(f"Aggiunti {len(files)} PDF via drag & drop.")

    def on_drop_img(self, event):
        files = self.parse_drop_files(event.data)
        for f in files:
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
                self.carica_immagine(f)
                break

    def on_drop(self, event):
        files = self.parse_drop_files(event.data)
        for f in files:
            if f.lower().endswith('.pdf'):
                if f not in self.pdf_files:
                    self.pdf_files.append(f)
            elif f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
                if self.watermark_path != f:
                    self.carica_immagine(f)
        self.aggiorna_lista_pdf()

    def parse_drop_files(self, data):
        files = []
        in_braces = False
        current = ''
        for ch in data:
            if ch == '{':
                in_braces = True
                current = ''
            elif ch == '}':
                in_braces = False
                if current:
                    files.append(current)
                current = ''
            elif ch == ' ' and not in_braces:
                if current:
                    files.append(current)
                    current = ''
            else:
                current += ch
        if current:
            files.append(current)
        return files

    # --------------------------------------------------------
    # Output directory
    # --------------------------------------------------------
    def scegli_output_dir(self):
        path = filedialog.askdirectory(title="Seleziona cartella di output")
        if path:
            self.output_dir_var.set(path)
            self.salva_config()

    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------
    def log(self, message):
        self.log_text.insert(END, message + "\n")
        self.log_text.see(END)

    # --------------------------------------------------------
    # Elaborazione (thread)
    # --------------------------------------------------------
    def avvia_elaborazione(self):
        if self.processing:
            return
        if not self.pdf_files:
            messagebox.showwarning("Attenzione", "Nessun PDF selezionato.")
            return
        if self.watermark_image is None:
            messagebox.showwarning("Attenzione", "Nessuna immagine filigrana caricata.")
            return

        out_dir = self.output_dir_var.get()
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir)
            except:
                messagebox.showerror("Errore", "Impossibile creare la cartella di output.")
                return

        # Raccogli parametri filigrana
        try:
            params = {
                'scale': float(self.scale_var.get()),
                'max_dx': float(self.dx_var.get()),
                'max_dy': float(self.dy_var.get()),
                'max_angle': float(self.angle_var.get()),
                'mode': self.mode_var.get(),
                'opacity': float(self.opacity_var.get()),
                'dpi': int(self.dpi_var.get()),
                'max_page_rotation': float(self.page_rot_var.get())
            }
        except ValueError as e:
            messagebox.showerror("Errore", f"Parametri non validi: {e}")
            return

        # Raccogli parametri effetti
        effetti_params = {
            'noise_enabled': self.noise_var.get(),
            'noise_intensity': self.noise_intensity.get(),
            'blur_enabled': self.blur_var.get(),
            'blur_radius': self.blur_radius.get(),
            'contrast_enabled': self.contrast_var.get(),
            'contrast_intensity': self.contrast_intensity.get(),
            'scanlines_enabled': self.scanlines_var.get(),
            'scanlines_opacity': self.scanlines_opacity.get(),
        }

        # Formato compressione
        formato_img = self.img_format_var.get()
        qualita_jpeg = self.jpeg_quality_var.get()

        self.processing = True
        self.btn_avvia.config(state=DISABLED)
        self.btn_interrompi.config(state=NORMAL)
        self.total_pdfs = len(self.pdf_files)
        self.current_pdf_index = 0
        self.progress['maximum'] = self.total_pdfs
        self.progress['value'] = 0

        # Salva configurazione prima di iniziare
        self.salva_config()

        self.thread = threading.Thread(target=self.processa_tutti,
                                        args=(params, effetti_params, formato_img, qualita_jpeg, out_dir))
        self.thread.daemon = True
        self.thread.start()

    def processa_tutti(self, params, effetti_params, formato_img, qualita_jpeg, out_dir):
        for pdf_path in self.pdf_files:
            if not self.processing:
                break
            self.queue_progress.put(('inizio', pdf_path))
            processa_pdf(pdf_path, self.watermark_image, out_dir, params, effetti_params,
                         formato_img, qualita_jpeg, self.queue_progress, self.queue_result)
        self.queue_result.put(('fine',))

    def processa_code(self):
        try:
            while True:
                msg = self.queue_progress.get_nowait()
                if isinstance(msg, tuple) and len(msg) == 3:
                    pdf_path, i, totale = msg
                    # aggiornamento dettagliato (opzionale)
                elif isinstance(msg, tuple) and msg[0] == 'inizio':
                    self.current_pdf_index += 1
                    self.log(f"Elaborazione {os.path.basename(msg[1])}...")
                self.progress['value'] = self.current_pdf_index
        except queue.Empty:
            pass

        try:
            while True:
                res = self.queue_result.get_nowait()
                if isinstance(res, tuple) and res[0] == 'fine':
                    self.processing = False
                    self.btn_avvia.config(state=NORMAL)
                    self.btn_interrompi.config(state=DISABLED)
                    self.progress['value'] = self.total_pdfs
                    self.log("Elaborazione completata.")
                    messagebox.showinfo("Completato", "Tutti i PDF sono stati elaborati.")
                else:
                    pdf_path, success, info = res
                    if success:
                        self.log(f"✓ {os.path.basename(pdf_path)} -> {info}")
                    else:
                        self.log(f"✗ {os.path.basename(pdf_path)} ERRORE: {info}")
        except queue.Empty:
            pass

        self.after(100, self.processa_code)

    def interrompi_elaborazione(self):
        if self.processing:
            self.processing = False
            self.log("Elaborazione interrotta dall'utente.")
            self.btn_avvia.config(state=NORMAL)
            self.btn_interrompi.config(state=DISABLED)

# ------------------------------------------------------------
# Avvio applicazione
# ------------------------------------------------------------
if __name__ == "__main__":
    app = PdfSporcaApp()
    app.mainloop()