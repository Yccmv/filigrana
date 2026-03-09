@echo off
REM ═══════════════════════════════════════════════════════════════
REM  compila.bat  —  Compila PDF Sporca in un singolo .exe
REM  Esegui questo file nella stessa cartella di filigrana.py
REM ═══════════════════════════════════════════════════════════════

echo Controllo dipendenze Python...
pip install pyinstaller pillow pypdf reportlab pymupdf numpy tkinterdnd2

echo.
echo Compilazione in corso...
pyinstaller filigrana.spec --clean

echo.
echo ─────────────────────────────────────────────
echo  Fatto! Il file si trova in:  dist\PDFSporca.exe
echo ─────────────────────────────────────────────
pause
