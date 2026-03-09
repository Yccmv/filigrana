# PDF Filigrana 🖨️

**PDF Filigrana** è uno strumento desktop per applicare filigrane e simulare l'aspetto di documenti scansionati su file PDF.

Sviluppato in Python con interfaccia grafica Tkinter.

---

## ✨ Funzionalità

- **Filigrana** — sovrappone un'immagine (PNG/JPG) su ogni pagina del PDF, con controllo su scala, opacità, posizione e rotazione
- **Effetti scansione realistica:**
  - Grain fotografico e rumore gaussiano
  - Sfocatura (messa a fuoco imperfetta)
  - Vignettatura (bordi più scuri)
  - Micro-ondulazione (deformazione scansione)
  - Macchie e sporco carta
  - Linee di scansione
  - Schiarimento/livelli
- **Anti-aliasing** avanzato per rotazioni senza scalettatura
- **Anteprima interattiva** con zoom e drag
- **Elaborazione multi-processo** per batch di PDF
- **Modalità colore** — output a colori, scala di grigi o bianco/nero
- **Salvataggio configurazione** automatico in `config.json`

---

## 🖥️ Requisiti

- Windows 10/11
- Python 3.10+ (solo per eseguire da sorgente)

### Dipendenze Python

```
pip install pillow pypdf reportlab pymupdf numpy tkinterdnd2
```

---

## 🚀 Utilizzo

### Versione compilata (Windows)
Scarica `PDFSporca.exe` dalla sezione [Releases](../../releases) e avvialo direttamente — nessuna installazione richiesta.

Il file `config.json` viene creato automaticamente nella stessa cartella dell'exe per salvare le impostazioni.

### Da sorgente
```bash
python filigrana.py
```

### Compilare l'exe da soli
```bash
pip install pyinstaller
pyinstaller filigrana.spec
# output: dist/PDFSporca.exe
```

---

## 📁 Struttura

```
filigrana.py       # Sorgente principale
filigrana.spec     # Configurazione PyInstaller
config.json        # Impostazioni salvate (generato automaticamente)
compila.bat        # Script Windows per compilare l'exe
```

---

## 📄 Licenza

GNU General Public License v3.0
