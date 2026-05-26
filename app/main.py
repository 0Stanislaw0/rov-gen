import yaml
import re
import urllib.parse
import traceback
import uuid
from datetime import datetime, timedelta 
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List
from pathlib import Path

from .docx_gen import generate_docx
from .config_loader import load_config

app = FastAPI()

# Определяем корень проекта (на уровень выше папки app)
BASE_DIR = Path(__file__).resolve().parent.parent

# Load Config
CONFIG = load_config(str(BASE_DIR / "config.yaml"))

class Stage(BaseModel):
    description: str
    duration_raw: str
    comment: str = ""

class ReleaseData(BaseModel):
    release_number: str
    start_date_time: str # Format: "dd.mm.yy HH:MM"
    as_system: str
    fp_system: str
    responsible: str
    contacts: str
    description: str
    possible_downtime: str
    stages: List[Stage]
    rollback_stages: List[Stage]

def parse_duration(raw: str) -> int:
    """Converts '1 hour', '60 min' etc to minutes."""
    raw = raw.lower()
    digits = re.findall(r'\d+', raw)
    if not digits:
        return 0
    val = int(digits[0])
    if 'час' in raw or 'hour' in raw:
        return val * 60
    return val

def parse_date(date_str: str) -> datetime:
    """Parses various date formats: 18.05.26, 18.05.2026, 18/05/26 etc."""
    clean_date = re.sub(r'[/-]', '.', date_str)
    # Пробуем разные форматы года (2 и 4 цифры)
    for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(clean_date, fmt)
        except ValueError:
            continue
    raise ValueError(f"Некорректный формат даты: {date_str}. Ожидается DD.MM.YY HH:MM или DD.MM.YYYY HH:MM")

@app.get("/config")
def get_config():
    # Создаем копию конфига, чтобы не мутировать глобальный объект
    res_config = CONFIG.copy()
    # Если в конфиге нет явного списка систем, берем ключи из fp_mapping
    if "as_list" not in res_config and "fp_mapping" in res_config:
        res_config["as_list"] = list(res_config["fp_mapping"].keys())
    return res_config

@app.post("/generate")
def generate_report(data: ReleaseData):
    try:
        # Валидация FP
        valid_fps = CONFIG.get("fp_mapping", {}).get(data.as_system, [])
        if data.fp_system not in valid_fps:
            raise ValueError(f"Система {data.fp_system} не входит в состав {data.as_system}")

        # Извлекаем дополнительные метаданные из конфига (скрытые от пользователя)
        system_meta = CONFIG.get("system_metadata", {}).get(data.as_system, {}).get(data.fp_system, {})
        
        start_dt = parse_date(data.start_date_time)
        current_time = start_dt
        
        def process_stages(stages_list, start_from):
            table = []
            t = start_from
            for idx, stage in enumerate(stages_list, 1):
                duration_min = parse_duration(stage.duration_raw)
                end_time = t + timedelta(minutes=duration_min)
                
                s_str = t.strftime('%d.%m.%y %H:%M')
                e_str = end_time.strftime('%d.%m.%y %H:%M')
                
                table.append({
                    "id": str(idx),
                    "work": stage.description.strip(),
                    "start_time": s_str,
                    "end_time": e_str,
                    "interval": f"{s_str} - {e_str}",
                    "comment": stage.comment.strip() if stage.comment else ""
                })
                t = end_time
            return table, t

        # 1. План работ
        work_table, current_time = process_stages(data.stages, start_dt)

        # 2. Точка принятия решения (DP)
        # Начало - сразу после работ
        dp_start = current_time
        # Конец - минимум +30 мин и округление вверх до ближайших 30 минут
        tmp_end = dp_start + timedelta(minutes=30)
        rem = tmp_end.minute % 30
        dp_end = tmp_end if rem == 0 else tmp_end + timedelta(minutes=(30 - rem))
        dp_end = dp_end.replace(second=0, microsecond=0)

        # 3. План отката (относительное время от точки Y)
        def format_relative(minutes):
            if minutes == 0:
                return "Y"
            h = minutes // 60
            m = minutes % 60
            return f"Y + {h:02d}:{m:02d}"

        rollback_table = []
        accumulated_mins = 0
        for idx, stage in enumerate(data.rollback_stages, 1):
            dur = parse_duration(stage.duration_raw)
            start_rel = format_relative(accumulated_mins)
            accumulated_mins += dur
            end_rel = format_relative(accumulated_mins)
            
            rollback_table.append({
                "id": str(idx),
                "work": stage.description.strip(),
                "start_time": start_rel,
                "end_time": end_rel,
                "interval": f"{start_rel} - {end_rel}",
                "comment": stage.comment.strip() if stage.comment else ""
            })

        context = {
            "RELEASE_NUMBER": str(data.release_number).strip(),
            "START_DATE": start_dt.strftime('%d.%m.%y'),
            "START_TIME": start_dt.strftime('%H:%M'),
            "AS": str(data.as_system).strip(),
            "FP": str(data.fp_system).strip(),
            "RESPONSIBLE": str(data.responsible).strip(),
            "CONTACTS": str(data.contacts).strip(),
            "DESCRIPTION": str(data.description).strip(),
            "POSSIBLE_DOWNTIME": str(data.possible_downtime).strip(),
            "WORK_TABLE": work_table,
            "ROLLBACK_TABLE": rollback_table,
            # Точка принятия решения
            "DP_START": dp_start.strftime('%H:%M'),
            "DP_END": dp_end.strftime('%H:%M'),
            # Метаданные из конфига
            "APPROVER_NAME": system_meta.get("approver_name", ""),
            "APPROVER_TITLE": system_meta.get("approver_title", ""),
            "OWNER_NAME": system_meta.get("owner_name", ""),
            "OWNER_CONTACTS": system_meta.get("owner_contacts", ""),
            "BLOCK_VAL": system_meta.get("block_val", "")
        }

        # Очищаем имя файла от запрещенных символов на всякий случай
        # Добавляем более жесткую очистку имени файла
        safe_fp = re.sub(r'[\\/*?:"<>|]', "_", data.fp_system)
        safe_release = re.sub(r'[\\/*?:"<>|]', "_", data.release_number)
        clean_filename = f"РОВ_{safe_fp}_{safe_release}.docx"
        
        # Генерируем УНИКАЛЬНОЕ имя для файла на диске, чтобы запросы не пересекались
        unique_disk_name = f"{uuid.uuid4().hex}.docx"
        file_path = BASE_DIR / unique_disk_name

        # Список возможных путей к шаблону для гибкости
        possible_paths = [
            BASE_DIR / "templates" / "template.docx",
            BASE_DIR / "app" / "templates" / "template.docx"
        ]
        
        template_path = next((p for p in possible_paths if p.exists()), None)

        if not template_path:
            raise FileNotFoundError(f"Шаблон template.docx не найден. Проверенные пути: {[str(p) for p in possible_paths]}")

        print(f"DEBUG: Начинаю генерацию. Файл будет тут: {file_path.absolute()}")

        # Проверяем, что файл не пустой
        if template_path.stat().st_size == 0:
            raise ValueError(f"Файл шаблона {template_path} пуст. Пожалуйста, замените его корректным документом Word .docx")

        try:
            generate_docx(str(template_path), str(file_path), context)
        except Exception as e:
            print(f"TRACEBACK: {traceback.format_exc()}")
            raise ValueError(f"Ошибка генерации DOCX: {str(e)}. Проверьте, что теги в шаблоне корректны и не разорваны форматированием.")
        
        # Проверяем, что файл был успешно создан и не пуст
        if not file_path.exists() or file_path.stat().st_size == 0:
            # Если файл был создан, но пуст, удаляем его
            if file_path.exists():
                file_path.unlink(missing_ok=True)
            raise ValueError("Сгенерированный файл пуст или не существует. Возможно, ошибка в процессе генерации DOCX.")
        
        print(f"DEBUG: Сгенерирован файл {file_path}, размер: {file_path.stat().st_size} байт")

        # Кодируем имя файла для безопасной передачи кириллицы
        encoded_name = urllib.parse.quote(clean_filename)
        content_disposition = f'attachment; filename="report.docx"; filename*=utf-8\'\'{encoded_name}'

        return FileResponse(
            path=str(file_path),
            media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            headers={
                "Content-Disposition": content_disposition,
                "Access-Control-Expose-Headers": "Content-Disposition",
                "Cache-Control": "no-cache"
            }
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/download/{filename}")
async def download_file(filename: str, pretty_name: str = None):
    # Декодируем имя файла, так как браузер может прислать его в URL-encoded виде (кириллица)
    decoded_filename = urllib.parse.unquote(filename)
    # Ограничиваем доступ только к файлам .docx в корне, предотвращая Path Traversal
    if ".." in decoded_filename or not decoded_filename.endswith(".docx"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Ищем файлы в корне проекта
    file_path = BASE_DIR / decoded_filename
    
    print(f"DEBUG: Попытка скачивания файла: {file_path}")
    
    if not file_path.exists():
        print(f"DEBUG: Файл не найден: {file_path}")
        raise HTTPException(status_code=404, detail=f"Файл {decoded_filename} не найден на сервере")
    
    # Если передано красивое имя, используем его, иначе используем имя файла на диске
    final_name = pretty_name if pretty_name else decoded_filename

    # Кодируем имя файла только для части filename* (стандарт RFC 5987)
    # В обычном filename оставляем ASCII-безопасное имя
    safe_name = "report.docx"
    encoded_name = urllib.parse.quote(final_name)
    content_disposition = f'attachment; filename="{safe_name}"; filename*=utf-8\'\'{encoded_name}'

    return FileResponse(
        path=str(file_path), 
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        headers={
            "Content-Disposition": content_disposition,
            "Cache-Control": "no-cache"
        }
    )

static_dir = BASE_DIR / "app" / "static"

@app.get("/")
async def read_index():
    return FileResponse(static_dir / "index.html")

app.mount("/static", StaticFiles(directory=static_dir), name="static")
