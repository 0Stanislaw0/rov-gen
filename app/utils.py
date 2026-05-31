import re
from datetime import datetime
from pathlib import Path

# Корень проекта (вычисляется относительно расположения файла utils.py)
BASE_DIR = Path(__file__).resolve().parent.parent

# Директории для хранения сгенерированных файлов
GENERATED_DIR = BASE_DIR / "generated"
DOCS_DIR = GENERATED_DIR / "docs"
EMLS_DIR = GENERATED_DIR / "emls"

# Создаем структуру папок, если она отсутствует
DOCS_DIR.mkdir(parents=True, exist_ok=True)
EMLS_DIR.mkdir(parents=True, exist_ok=True)

def parse_duration(raw: str) -> int:
    """Преобразует строку длительности в минуты."""
    raw = raw.lower()
    digits = re.findall(r'\d+', raw)
    if not digits:
        return 0
    val = int(digits[0])
    if 'час' in raw or 'hour' in raw:
        return val * 60
    return val

def parse_date(date_str: str) -> datetime:
    """Парсит различные форматы дат."""
    clean_date = re.sub(r'[/-]', '.', date_str)
    for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(clean_date, fmt)
        except ValueError:
            continue
    raise ValueError(f"Некорректный формат даты: {date_str}")

def get_safe_filename(name: str) -> str:
    """Удаляет спецсимволы, которые запрещены в именах файлов различных ОС."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)