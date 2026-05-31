"""
book_to_audio.py
Конвертирует PDF, EPUB, FB2 книги в MP3 по главам.
Голос: ru-RU-DmitryNeural (Microsoft Edge TTS, требует интернет)

При запуске появляются диалоги выбора папок и настройки скорости.
"""

import os
import re
import sys
import asyncio
import subprocess
import json
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import fitz  # pymupdf
import edge_tts
from ebooklib import epub
from bs4 import BeautifulSoup

# ─── Настройки ────────────────────────────────────────────────────────────────
VOICE = "ru-RU-DmitryNeural"
MAX_CHUNK = 3000
MIN_CHAPTER_CHARS = 300

# Словарь ручных замен — читается как "произносится"
# Формат: "слово": "правильное произношение"
REPLACEMENTS = {
    # "PDF":  "пэ дэ эф",
    # "HTTP": "эйч ти ти пи",
}

# Путь к файлу с пользовательскими заменами
USER_REPLACEMENTS_FILE = Path(__file__).parent / "user_replacements.json"


def load_user_replacements():
    """Загружает пользовательские замены из файла."""
    if USER_REPLACEMENTS_FILE.exists():
        try:
            with open(USER_REPLACEMENTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_user_replacements(replacements):
    """Сохраняет пользовательские замены в файл."""
    with open(USER_REPLACEMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(replacements, f, ensure_ascii=False, indent=2)


def get_all_replacements():
    """Возвращает объединённый словарь замен."""
    combined = dict(REPLACEMENTS)
    combined.update(load_user_replacements())
    return combined


def add_user_replacement(bad_word, good_pronunciation):
    """Добавляет пользовательскую замену и сохраняет."""
    replacements = load_user_replacements()
    replacements[bad_word] = good_pronunciation
    save_user_replacements(replacements)
    return replacements
# ──────────────────────────────────────────────────────────────────────────────


# ─── Диалог запуска ───────────────────────────────────────────────────────────

def ask_settings():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    messagebox.showinfo("Шаг 1 из 3", "Выберите папку с книгами (PDF, EPUB, FB2)", parent=root)
    input_str = filedialog.askdirectory(title="Папка с книгами", parent=root)
    if not input_str:
        messagebox.showerror("Отмена", "Папка с книгами не выбрана. Выход.")
        sys.exit(0)

    messagebox.showinfo("Шаг 2 из 3", "Выберите папку куда сохранять аудиофайлы", parent=root)
    output_str = filedialog.askdirectory(title="Папка для аудиокниг", parent=root)
    if not output_str:
        messagebox.showerror("Отмена", "Папка для сохранения не выбрана. Выход.")
        sys.exit(0)

    speed_str = simpledialog.askstring(
        "Шаг 3 из 3 — Скорость чтения",
        "Введите скорость чтения:\n\n"
        "  0%   — стандартная\n"
        " -5%   — чуть медленнее (рекомендуется)\n"
        "-10%   — заметно медленнее\n"
        "-15%   — медленно\n"
        "+10%   — быстрее\n",
        parent=root,
        initialvalue="-5%"
    )
    if not speed_str:
        speed_str = "-5%"

    root.destroy()
    return Path(input_str), Path(output_str), speed_str.strip()


# ─── Очистка текста ───────────────────────────────────────────────────────────

def clean_text(text, replacements=None):
    """Убирает мусор и спецсимволы."""

    if replacements is None:
        replacements = get_all_replacements()

    # Склеиваем перенос слов через дефис в конце строки
    text = re.sub(r'(\w+)-\n(\w+)', r'\1\2', text)
    text = re.sub(r'(\w+)-\s+\n(\w+)', r'\1\2', text)

    # Убираем сноски (* ** и Прим. ред./пер.)
    text = re.sub(r'(?m)^\s*\*+.*$', '', text)
    text = re.sub(r'(?m)^\s*\d+\).*$', '', text)
    text = re.sub(r'—\s*Прим\.\s*(ред|пер)\.[^\n]*', '', text)

    # URL и email
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)
    text = re.sub(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', '', text)

    # Markdown
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#{1,6}\s?', '', text)
    text = re.sub(r'_{2,}', '', text)
    text = re.sub(r'\[.*?\]', '', text)

    # Служебные символы
    text = re.sub(r'[©®™°|\\^~`]', '', text)

    # Нормализация знаков препинания
    text = re.sub(r'[«»„""]', '"', text)
    text = re.sub(r'-{2,}', '—', text)
    text = re.sub(r'\.{2,}', '…', text)
    text = re.sub(r'[!?]{2,}', lambda m: m.group()[0], text)

    # Номера страниц (одиночное число на отдельной строке)
    text = re.sub(r'(?m)^\s*\d{1,4}\s*$', '', text)

    # Ручные замены
    for word, replacement in replacements.items():
        text = text.replace(word, replacement)

    # Пробелы
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Одиночный перенос внутри абзаца → пробел
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

    return text.strip()


def prepare_for_tts(text, title=None):
    """Готовит текст для передачи в edge-tts."""
    text = re.sub(r'\n\n+', '. ', text)
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r' {2,}', ' ', text)

    if title:
        text = f"{title}. {text}"

    return text.strip()


# ─── Разбивка на куски ────────────────────────────────────────────────────────

def split_into_chunks(text, max_len=MAX_CHUNK):
    """Разбивает текст на куски по границам предложений."""
    sentences = re.split(r'(?<=[.!?…])\s+', text)
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) < max_len:
            current += " " + s
        else:
            if current:
                chunks.append(current.strip())
            current = s
    if current:
        chunks.append(current.strip())
    return chunks if chunks else [text]


def play_audio_file(file_path):
    """Воспроизводит аудиофайл."""
    file_path = Path(file_path)
    if not file_path.exists():
        return False

    if sys.platform == "win32":
        subprocess.run(["start", "/wait", str(file_path)], shell=True, check=False)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(file_path)], check=False)
    else:
        subprocess.run(["xdg-open", str(file_path)], check=False)
    return True


def interactive_fix_mode(chapter_num, title, text, mp3_path, rate):
    """
    Интерактивный режим исправления главы.
    Возвращает True если глава была переозвучена.
    """
    print(f"\n  🎧 [{chapter_num}] {title[:50]}...")
    print(f"     Файл: {mp3_path.name}")

    need_reexport = False

    while True:
        # Открываем аудиофайл для прослушивания
        play_audio_file(mp3_path)

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        # Спрашиваем результат
        result = messagebox.askyesno(
            "Проверка озвучки",
            f"Глава: {title[:60]}\n\n"
            f"Всё OK? ('Да' = принять, 'Нет' = исправить слово)",
            parent=root
        )
        root.destroy()

        if result:
            return need_reexport

        # Запрашиваем проблемное слово
        root2 = tk.Tk()
        root2.withdraw()
        root2.attributes("-topmost", True)

        dialog = tk.Toplevel(root2)
        dialog.title("Добавить замену")
        dialog.geometry("500x220")
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        frame = tk.Frame(dialog, padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="Слово которое читается НЕПРАВИЛЬНО:",
                 font=("Arial", 10)).grid(row=0, column=0, columnspan=2, sticky=tk.W)
        bad_entry = tk.Entry(frame, width=40, font=("Arial", 12))
        bad_entry.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(0, 10))

        tk.Label(frame, text="ПРАВИЛЬНОЕ произношение (фонетически):",
                 font=("Arial", 10)).grid(row=2, column=0, columnspan=2, sticky=tk.W)
        good_entry = tk.Entry(frame, width=40, font=("Arial", 12))
        good_entry.grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=(0, 10))

        def do_save():
            dialog.result = (bad_entry.get().strip(), good_entry.get().strip())
            dialog.destroy()

        def do_skip():
            dialog.result = None
            dialog.destroy()

        def do_play():
            play_audio_file(mp3_path)

        btn_frame = tk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=(5, 0))

        tk.Button(btn_frame, text="Сохранить и переозвучить", command=do_save,
                  font=("Arial", 10), bg="#90EE90", width=20).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="Повторить аудио", command=do_play,
                  font=("Arial", 10), bg="#ADD8E6", width=12).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="Пропустить", command=do_skip,
                  font=("Arial", 10), bg="#FFB6C1", width=10).pack(side=tk.LEFT, padx=2)

        frame.columnconfigure(0, weight=1)

        dialog.wait_window()
        root2.destroy()

        if dialog.result is None:
            return need_reexport

        bad_word, good_word = dialog.result

        if bad_word and good_word:
            add_user_replacement(bad_word, good_word)
            print(f"     📝 Добавлено: '{bad_word}' → '{good_word}'")
            need_reexport = True
            return True  # Нужна переозвучка
        elif bad_word:
            messagebox.showerror("Ошибка", "Нужно заполнить оба поля!", parent=None)
            continue

    return need_reexport


# ─── Озвучка ──────────────────────────────────────────────────────────────────

async def text_to_mp3(text, out_path, rate, title=None):
    """Озвучивает текст через edge-tts и сохраняет в MP3."""
    prepared = prepare_for_tts(text, title=title)
    chunks = split_into_chunks(prepared)
    tmp_files = []

    for i, chunk in enumerate(chunks):
        tmp = out_path.with_suffix(f".part{i}.mp3")
        communicate = edge_tts.Communicate(chunk, VOICE, rate=rate)
        await communicate.save(str(tmp))
        tmp_files.append(str(tmp))

    if len(tmp_files) == 1:
        os.replace(tmp_files[0], out_path)
    else:
        list_file = out_path.with_suffix(".list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for t in tmp_files:
                f.write(f"file '{t}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(list_file), "-c", "copy", str(out_path)],
            capture_output=True
        )
        list_file.unlink(missing_ok=True)
        for t in tmp_files:
            Path(t).unlink(missing_ok=True)


# ─── Извлечение глав ──────────────────────────────────────────────────────────

def extract_epub_chapters(book_path):
    book = epub.read_epub(str(book_path))
    chapters = []
    for i, item in enumerate(book.get_items_of_type(9)):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        heading = soup.find(re.compile(r'^h[1-3]$'))
        title = heading.get_text(strip=True) if heading else f"Глава {i+1}"
        text = clean_text(soup.get_text(separator="\n"))
        if len(text) >= MIN_CHAPTER_CHARS:
            chapters.append((title, text))
    return chapters


def extract_fb2_chapters(book_path):
    tmp_epub = book_path.with_suffix(".tmp_converted.epub")
    result = subprocess.run(
        ["ebook-convert", str(book_path), str(tmp_epub)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [!] Ошибка конвертации FB2: {result.stderr[:200]}")
        return []
    chapters = extract_epub_chapters(tmp_epub)
    tmp_epub.unlink(missing_ok=True)
    return chapters


def extract_pdf_chapters(book_path):
    doc = fitz.open(str(book_path))
    toc = doc.get_toc()

    if toc:
        chapters = []
        for idx, (level, title, page) in enumerate(toc):
            if level > 2:
                continue
            next_page = toc[idx + 1][2] if idx + 1 < len(toc) else len(doc)
            text = ""
            for p in range(page - 1, min(next_page - 1, len(doc))):
                text += doc[p].get_text()
            text = clean_text(text)
            if len(text) >= MIN_CHAPTER_CHARS:
                chapters.append((clean_text(title), text))
        if chapters:
            return chapters

    pages_per_chunk = 20
    chapters = []
    total = len(doc)
    for start in range(0, total, pages_per_chunk):
        end = min(start + pages_per_chunk, total)
        text = ""
        for p in range(start, end):
            text += doc[p].get_text()
        text = clean_text(text)
        if len(text) >= MIN_CHAPTER_CHARS:
            chapters.append((f"Страницы {start+1}–{end}", text))
    return chapters


# ─── Основная логика ──────────────────────────────────────────────────────────

def get_book_title(book_path):
    name = book_path.stem
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    return name


async def process_book(book_path, output_root, rate):
    ext = book_path.suffix.lower()
    print(f"\n📖 Обрабатываю: {book_path.name}")

    if ext == ".epub":
        chapters = extract_epub_chapters(book_path)
    elif ext == ".fb2":
        chapters = extract_fb2_chapters(book_path)
    elif ext == ".pdf":
        chapters = extract_pdf_chapters(book_path)
    else:
        print(f"  [!] Неизвестный формат, пропускаю.")
        return

    if not chapters:
        print(f"  [!] Не удалось извлечь главы, пропускаю.")
        return

    book_dir = output_root / get_book_title(book_path)
    book_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Найдено глав: {len(chapters)}")
    print(f"  Сохраняю в: {book_dir}")

    for i, (title, text) in enumerate(chapters, 1):
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:60]
        mp3_name = f"{i:03d}. {safe_title}.mp3"
        mp3_path = book_dir / mp3_name

        if mp3_path.exists():
            print(f"  ✓ Уже есть: {mp3_name}")
            continue

        current_text = clean_text(text)
        need_reexport = False

        while True:
            print(f"  🔊 [{i}/{len(chapters)}] {title[:50]}...")
            try:
                await text_to_mp3(current_text, mp3_path, rate, title=title)
                print(f"     Сохранено: {mp3_name}")
            except Exception as e:
                print(f"     [!] Ошибка: {e}")
                break

            # Интерактивная проверка
            need_reexport = interactive_fix_mode(i, title, current_text, mp3_path, rate)
            if not need_reexport:
                break

            # Переозвучка с новыми заменами
            current_text = clean_text(text)  # Перечитать текст с новыми заменами
            mp3_path.unlink(missing_ok=True)

        print(f"     ✓ Глава {i} завершена")


async def main():
    input_dir, output_dir, rate = ask_settings()
    output_dir.mkdir(parents=True, exist_ok=True)

    extensions = {".pdf", ".epub", ".fb2"}
    books = [f for f in input_dir.iterdir() if f.suffix.lower() in extensions]

    if not books:
        print(f"Книги не найдены в папке: {input_dir}")
        sys.exit(1)

    print(f"Найдено книг: {len(books)}")
    print(f"Голос: {VOICE}")
    print(f"Скорость: {rate}")
    print(f"Выходная папка: {output_dir}\n")

    for book in sorted(books):
        await process_book(book, output_dir, rate)

    print("\n✅ Готово!")


if __name__ == "__main__":
    asyncio.run(main())