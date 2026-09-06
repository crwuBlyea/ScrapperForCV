# image_utils.py
import hashlib
from pathlib import Path
from PIL import Image
import io
import csv

MIN_FILE_SIZE = 20000  # 20KB
MIN_IMAGE_DIMENSION = 512
MAX_DIMENSION = 1024

HASH_CACHE: set[str] = set()

def load_hash_cache_from_csv(metadata_path: Path) -> None:
    """Загружает существующие хеши из metadata.csv."""
    if not metadata_path.exists():
        return
    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                h = row.get('hash')
                if h:
                    HASH_CACHE.add(h)
        print(f"✅ Загружено {len(HASH_CACHE)} существующих хешей из metadata.")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить кэш метаданных: {e}")

def process_and_save_image(data: bytes, image_url: str, label: str, save_path: Path) -> dict | None:
    """Проверяет, сжимает и сохраняет изображение в WebP."""
    if len(data) < MIN_FILE_SIZE:
        return None
    
    stripped = data[:512].lstrip()
    html_signatures = (b'<html', b'<!DOCTYPE', b'<?xml', b'<HTML', b'<!doctype')
    if any(stripped.startswith(sig) for sig in html_signatures):
        return None

    file_hash = hashlib.md5(data).hexdigest()
    if file_hash in HASH_CACHE:
        return None
    HASH_CACHE.add(file_hash)

    image: Image.Image | None = None
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        
        if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
            return None
        
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")

        if max(width, height) > MAX_DIMENSION:
            ratio = MAX_DIMENSION / max(width, height)
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

        save_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(save_path, format="WEBP", quality=96, method=6)

        if not save_path.exists() or save_path.stat().st_size < MIN_FILE_SIZE:
            if save_path.exists():
                save_path.unlink()
            return None

        return {
            "id": hashlib.md5(image_url.encode()).hexdigest()[:16],
            "filename": save_path.name,
            "label": label,
            "image_url": image_url,
            "width": image.width,
            "height": image.height,
            "size_bytes": save_path.stat().st_size,
            "hash": file_hash,
        }
    except Exception as e:
        print(f"⚠️ Не удалось обработать изображение {image_url}: {e}")
        return None
    finally:
        if image is not None:
            image.close()