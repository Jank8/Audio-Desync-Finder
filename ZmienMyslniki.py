"""
Zmienia myślnik na pauzę w nazwach plików filmowych.

    python ZmienMyslniki.py "D:\\Filmy"           # pokazuje, co by zmienił
    python ZmienMyslniki.py "D:\\Filmy" --apply   # zmienia
    python ZmienMyslniki.py --undo                # cofa ostatnią zmianę

Bez `--apply` nic nie rusza - wypisuje tylko listę.  Każda zmiana jest zapisana
w `zmien-myslniki-undo.json` obok tego skryptu, więc `--undo` przywraca poprzednie
nazwy co do znaku.

Dotyczy tylko nazw plików: ` - ` (myślnik ze spacjami) staje się ` – ` (pauza).
Myślniki bez spacji, na przykład `MPLEX-4K-HDR`, zostają nietknięte - to część
nazwy wydania, a nie typografia.
"""

import argparse
import json
import os
import re
import sys

MEDIA = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".mpg",
         ".mka", ".flac", ".m4a", ".srt", ".ass", ".nfo")
UNDO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "zmien-myslniki-undo.json")


def convert(name: str, loose: bool = False) -> str:
    """
    Myślnik na pauzę.

    Domyślnie zmienia tylko ` - ` (myślnik ze spacjami), bo to jest typografia.
    Z `loose=True` zmienia każdy myślnik poza nawiasami - `[MPLEX-4K-HDR]` i
    `(Directors-Cut)` to oznaczenia wydania i zostają nietknięte.
    """
    stem, extension = os.path.splitext(name)
    guarded = []

    def hide(match):
        guarded.append(match.group(0))
        return f"\x00{len(guarded) - 1}\x00"

    protected = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", hide, stem)
    protected = protected.replace(" - ", " – ")
    if loose:
        protected = protected.replace("-", "–")
    for index, original in enumerate(guarded):
        protected = protected.replace(f"\x00{index}\x00", original)
    return protected + extension


def collect(root: str, loose: bool = False):
    """Pliki filmowe pod *root*, których nazwa naprawdę by się zmieniła."""
    found = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(MEDIA):
                continue
            if convert(name, loose) != name:
                found.append(os.path.join(base, name))
    return sorted(found)


def apply_renames(paths, undo, loose: bool = False):
    done, failed = [], []
    for path in paths:
        directory, name = os.path.split(path)
        target = os.path.join(directory, convert(name, loose))
        if os.path.exists(target):
            failed.append((path, "taki plik już jest"))
            continue
        try:
            os.rename(path, target)
        except OSError as error:
            failed.append((path, str(error)))
            continue
        done.append((path, target))
        undo.setdefault("renames", []).append([path, target])
        print(f"  {name}\n    -> {os.path.basename(target)}")
    with open(UNDO_FILE, "w", encoding="utf-8") as handle:
        json.dump(undo, handle, ensure_ascii=False, indent=1)
    return done, failed


def undo_last():
    if not os.path.exists(UNDO_FILE):
        print("Nie ma czego cofać - brak pliku z listą zmian.")
        return 1
    with open(UNDO_FILE, encoding="utf-8") as handle:
        undo = json.load(handle)
    renames = undo.get("renames") or []
    if not renames:
        print("Lista zmian jest pusta.")
        return 1
    restored = 0
    for original, renamed in reversed(renames):
        if os.path.exists(renamed) and not os.path.exists(original):
            try:
                os.rename(renamed, original)
                restored += 1
            except OSError as error:
                print(f"  nie udało się: {renamed} ({error})")
    undo["renames"] = []
    with open(UNDO_FILE, "w", encoding="utf-8") as handle:
        json.dump(undo, handle, ensure_ascii=False, indent=1)
    print(f"Przywrócono {restored} nazw.")
    return 0


def run_window():
    """
    Double-clicking the file has to work too: ask for the folder, ask whether the
    bare hyphens count as well, show what would change and only then rename.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception as error:
        print(f"Brak okienek (tkinter): {error}\nPodaj katalog w argumentach.")
        return 2
    root = tk.Tk()
    root.withdraw()
    folder = filedialog.askdirectory(title="Katalog z filmami do zmiany nazw")
    if not folder:
        return 0
    paths = collect(folder, False)
    if not paths:
        messagebox.showinfo(
            "ZmienMyslniki",
            f"W tym katalogu nie ma nazw z myślnikiem.\n\n{folder}")
        return 0
    loose = messagebox.askyesno(
        "ZmienMyslniki",
        "Zmieniać także myślniki BEZ spacji?\n\n"
        "Tak  - każdy myślnik, np. 'Film-2026-PL' -> 'Film–2026–PL'\n"
        "       (nawiasy zostają: [MPLEX-4K-HDR] bez zmian)\n"
        "Nie  - tylko ' - ' ze spacjami wokół")
    preview = "\n".join(
        f"• {os.path.basename(p)}\n     -> {convert(os.path.basename(p), loose)}"
        for p in paths[:20])
    if len(paths) > 20:
        preview += f"\n… i jeszcze {len(paths) - 20}"
    if not messagebox.askyesno(
            "ZmienMyslniki",
            f"Znalezione: {len(paths)}\n\n{preview}\n\nZmienić nazwy?"):
        return 0
    undo = {}
    if os.path.exists(UNDO_FILE):
        try:
            with open(UNDO_FILE, encoding="utf-8") as handle:
                undo = json.load(handle)
        except (OSError, ValueError):
            undo = {}
    done, failed = apply_renames(paths, undo, loose)
    messagebox.showinfo(
        "ZmienMyslniki",
        f"Zmienione: {len(done)}\nPominięte: {len(failed)}\n\n"
        + ("Cofnąć można, uruchamiając plik z --undo." if done else ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("folder", nargs="?", default=None,
                        help="katalog do przejrzenia (rekurencyjnie); bez niego "
                             "otwiera się okno wyboru katalogu")
    parser.add_argument("--apply", action="store_true",
                        help="naprawdę zmień nazwy (bez tego tylko pokazuje)")
    parser.add_argument("--undo", action="store_true",
                        help="cofnij ostatnią zmianę")
    parser.add_argument("--loose", action="store_true",
                        help="zmień też myślniki bez spacji (poza nawiasami)")
    args = parser.parse_args(argv)

    if args.folder is None and not args.apply and not args.undo:
        return run_window()
    if args.folder is None:
        args.folder = os.getcwd()

    if args.undo:
        return undo_last()

    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(f"To nie jest katalog: {root}")
        return 2
    paths = collect(root, args.loose)
    if not paths:
        print(f"Nic do zmiany w {root} - żadna nazwa nie ma myślnika.")
        return 0

    print(f"Znalezione w {root}: {len(paths)}")
    if not args.apply:
        for path in paths:
            print(f"  {os.path.basename(path)}\n"
                  f"    -> {convert(os.path.basename(path), args.loose)}")
        print("\nTo była tylko próba. Dodaj --apply, żeby zmienić nazwy.")
        return 0

    undo = {}
    if os.path.exists(UNDO_FILE):
        try:
            with open(UNDO_FILE, encoding="utf-8") as handle:
                undo = json.load(handle)
        except (OSError, ValueError):
            undo = {}
    done, failed = apply_renames(paths, undo, args.loose)
    print(f"\nZmienione: {len(done)}")
    for path, reason in failed:
        print(f"  pominięte: {os.path.basename(path)} ({reason})")
    if done:
        print(f"Cofać można przez: python {os.path.basename(__file__)} --undo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
