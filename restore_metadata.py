# restore_metadata.py
"""
Восстанавливает метаданные из существующих изображений в папках images/ai и images/human.
Синхронизирует metadata.csv с файловой системой (добавляет недостающие, опционально удаляет потерянные).
"""
import csv
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image

# Пути (те же, что в config.py)
BASE_DIR = Path("Path")
IMAGES_ROOT = BASE_DIR / "images"
METADATA_FILE = BASE_DIR / "metadata.csv"

# Полный набор полей в том порядке, в котором их пишет main.py
FIELDS = [
    "id", "filename", "label", "image_url", "width", "height",
    "size_bytes", "hash", "subreddit", "post_url", "timestamp", "source"
]


def get_file_hash(file_path: Path) -> str | None:
    """Вычисляет MD5-хеш файла."""
    try:
        with open(file_path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        print(f"   ⚠️ Не удалось прочитать хеш {file_path.name}: {e}")
        return None


def get_image_dimensions(file_path: Path) -> tuple[int, int]:
    """Получает размеры изображения через PIL."""
    try:
        with Image.open(file_path) as img:
            return img.width, img.height
    except Exception as e:
        print(f"   ⚠️ Не удалось открыть PIL {file_path.name}: {e}")
        return 0, 0


def load_existing_metadata() -> tuple[set[str], set[str], list[dict]]:
    """
    Загружает существующий metadata.csv.
    Возвращает: (set хешей, set имён файлов, список всех записей).
    """
    existing_hashes: set[str] = set()
    existing_filenames: set[str] = set()
    rows: list[dict] = []

    if not METADATA_FILE.exists() or METADATA_FILE.stat().st_size == 0:
        return existing_hashes, existing_filenames, rows

    try:
        with open(METADATA_FILE, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
                if row.get("hash"):
                    existing_hashes.add(row["hash"])
                if row.get("filename"):
                    existing_filenames.add(row["filename"])
        print(f"📄 Загружено {len(rows)} записей из metadata.csv")
    except Exception as e:
        print(f"⚠️ Ошибка при чтении metadata.csv: {e}")

    return existing_hashes, existing_filenames, rows


def discover_image_files() -> list[Path]:
    """
    Рекурсивно ищет все изображения в images/ai и images/human.
    """
    extensions = ("*.webp", "*.jpg", "*.jpeg", "*.png")
    files: list[Path] = []

    for subdir in ("ai", "human"):
        dir_path = IMAGES_ROOT / subdir
        if not dir_path.exists():
            print(f"⚠️ Папка {dir_path} не найдена, пропускаем")
            continue
        for ext in extensions:
            files.extend(dir_path.rglob(ext))

    return files


def restore_metadata(remove_orphaned: bool = False) -> None:
    """
    Восстанавливает метаданные из существующих файлов.

    Args:
        remove_orphaned: Если True, удаляет из CSV записи, для которых
                         файлов больше нет на диске (перезаписывает файл).
    """
    print("🔧 Восстановление метаданных из папок с картинками")
    print("=" * 55)

    # 1. Загружаем текущий CSV
    existing_hashes, existing_filenames, existing_rows = load_existing_metadata()

    # 2. Находим все файлы на диске
    image_files = discover_image_files()
    disk_filenames = {p.name for p in image_files}
    print(f"📁 Найдено {len(image_files)} файлов на диске")

    # 3. Собираем новые метаданные
    new_metadata: list[dict] = []
    skipped_by_filename = 0
    skipped_by_hash = 0
    skipped_by_read_error = 0
    restored = 0

    for file_path in image_files:
        filename = file_path.name

        # 3a. Пропуск по имени файла (уже есть в CSV)
        if filename in existing_filenames:
            skipped_by_filename += 1
            continue

        # 3b. Вычисляем хеш
        file_hash = get_file_hash(file_path)
        if file_hash is None:
            skipped_by_read_error += 1
            continue

        # 3c. Пропуск по хешу (файл переименован, но дублируется)
        if file_hash in existing_hashes:
            skipped_by_hash += 1
            print(f"   🔄 Пропущен {filename} — дубль по хешу (файл переименован?)")
            continue

        # 3d. Собираем метаданные
        width, height = get_image_dimensions(file_path)
        size_bytes = file_path.stat().st_size

        # Определяем label по родительской папке
        label = "human" if "human" in file_path.parts else "ai"
        subdir = "human" if label == "human" else "ai"

        # image_url — локальный относительный путь, чтобы id был детерминированным
        rel_path = f"{subdir}/{filename}"

        metadata = {
            "id": hashlib.md5(rel_path.encode()).hexdigest()[:16],
            "filename": filename,
            "label": label,
            "image_url": rel_path,
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "hash": file_hash,
            "subreddit": "restored",
            "post_url": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "restored"
        }

        new_metadata.append(metadata)
        existing_hashes.add(file_hash)
        existing_filenames.add(filename)
        restored += 1

        if restored % 50 == 0:
            print(f"   🔄 Обработано {restored} новых файлов...")

    # 4. Отчёт по добавлению
    print(f"\n📊 Статистика сканирования:")
    print(f"   ✅ Добавлено новых записей:     {restored}")
    print(f"   ⏭️ Уже в CSV (по имени):        {skipped_by_filename}")
    print(f"   ⏭️ Дубли по хешу (переименован): {skipped_by_hash}")
    print(f"   ❌ Ошибки чтения:               {skipped_by_read_error}")

    # 5. Обработка потерянных записей (orphaned)
    orphaned_rows = [row for row in existing_rows if row.get("filename") not in disk_filenames]
    if orphaned_rows:
        print(f"   🗑️ Записей без файла на диске:  {len(orphaned_rows)}")
        if remove_orphaned:
            print("   🧹 Удаление потерянных записей включено...")

    # 6. Запись в CSV
    if new_metadata or (remove_orphaned and orphaned_rows):
        if remove_orphaned:
            # Перезаписываем файл: старые записи + новые, минус orphaned
            kept_rows = [row for row in existing_rows if row.get("filename") in disk_filenames]
            all_rows = kept_rows + new_metadata
            print(f"💾 Перезапись metadata.csv ({len(all_rows)} записей)...")
            with open(METADATA_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                for row in all_rows:
                    # Убеждаемся, что все ключи присутствуют
                    clean_row = {k: row.get(k, "") for k in FIELDS}
                    writer.writerow(clean_row)
        else:
            # Дописываем в конец
            file_exists = METADATA_FILE.exists() and METADATA_FILE.stat().st_size > 0
            with open(METADATA_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                if not file_exists:
                    writer.writeheader()
                for row in new_metadata:
                    writer.writerow(row)
            print(f"💾 Дописано {len(new_metadata)} записей в {METADATA_FILE}")
    else:
        print("💾 Нет новых файлов для добавления")

    # 7. Итоговая сверка
    print("\n🔍 Итоговая сверка:")
    print(f"   Файлов на диске:    {len(disk_filenames)}")
    total_in_csv = len(existing_rows) + restored - (len(orphaned_rows) if remove_orphaned else 0)
    print(f"   Записей в CSV:      ~{total_in_csv}")
    if len(disk_filenames) != total_in_csv and not remove_orphaned:
        print(f"   ⚠️ Расхождение! Запустите с remove_orphaned=True для синхронизации.")


if __name__ == "__main__":
    # Чтобы удалить записи о несуществующих файлах, передайте remove_orphaned=True
    restore_metadata(remove_orphaned=True)