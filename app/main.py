import urllib.parse
import traceback
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pathlib import Path

from .models import ReleaseData
from .utils import BASE_DIR
from .services import (
    CONFIG, TEMPLATES_FILE, load_history, add_to_history,
    prepare_release_context, get_filenames, create_eml_content, 
    generate_docx, load_templates
)

app = FastAPI()
static_dir = BASE_DIR / "app" / "static"

@app.get("/config")
def get_config():
    """Отдает фронтенду базовые настройки и список доступных систем."""
    res_config = CONFIG.copy()
    if "as_list" not in res_config and "fp_mapping" in res_config:
        res_config["as_list"] = list(res_config["fp_mapping"].keys())
    return res_config

@app.get("/history")
def get_history_list():
    """Возвращает краткий список истории для отображения в таблице на фронтенде."""
    return [
        { 
            "id": i.id, 
            "timestamp": i.timestamp, 
            "release_number": i.release_data.release_number,
            "as_system": i.release_data.as_system, 
            "fp_system": i.release_data.fp_system, 
            "generated_filename": i.generated_filename,
            "disk_filename": i.disk_filename,
            "email_generated_filename": i.email_generated_filename,
            "email_disk_filename": i.email_disk_filename
        } for i in load_history()
    ]

@app.get("/history/{item_id}")
def get_history_item_data(item_id: str):
    """Возвращает полные исходные данные записи для предзаполнения формы (редактирование)."""
    for item in load_history():
        if item.id == item_id: return item.release_data
    raise HTTPException(status_code=404, detail="Запись истории не найдена")

@app.get("/templates")
def get_templates():
    return load_templates()

@app.post("/generate")
def generate_report(data: ReleaseData):
    """
    Основной процесс: расчет данных, генерация DOCX и EML, сохранение в историю.
    """
    try:
        context, system_meta = prepare_release_context(data)
        clean_docx, disk_docx, clean_eml, disk_eml = get_filenames(data)
        
        # Берем имя шаблона из данных или используем стандартный
        tpl_name = data.docx_template or "template.docx"
        tpl = BASE_DIR / "templates" / Path(tpl_name).name

        if not tpl.exists():
            raise FileNotFoundError(f"Шаблон не найден: {tpl}")

        generate_docx(str(tpl), str(BASE_DIR / disk_docx), context)
        msg = create_eml_content(data, system_meta, context)
        with open(BASE_DIR / disk_eml, 'wb') as f: f.write(msg.as_bytes())

        add_to_history(data, clean_docx, disk_docx, clean_eml, disk_eml)

        return FileResponse(path=str(BASE_DIR / disk_docx), filename=clean_docx)
    except Exception as e:
        print(traceback.format_exc())
        # Возвращаем текст ошибки, чтобы было понятно, что именно не так (например, формат даты)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/generate-email")
def generate_email_only(data: ReleaseData):
    """
    Генерация только EML файла без сохранения записи в историю.
    """
    try:
        context, system_meta = prepare_release_context(data)
        _, _, clean_eml, disk_eml = get_filenames(data)
        
        msg = create_eml_content(data, system_meta, context)
        
        # Сохраняем файл на диск, чтобы отдать его через FileResponse
        file_path = BASE_DIR / disk_eml
        with open(file_path, 'wb') as f:
            f.write(msg.as_bytes())
            
        return FileResponse(
            path=str(file_path), 
            filename=clean_eml, 
            media_type='message/rfc822'
        )
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/download/{filename:path}")
async def download_file(filename: str, pretty_name: str = None):
    """
    Скачивание файлов. 
    Параметр :path позволяет принимать относительные пути, например 'generated/docs/uuid.docx'.
    """
    decoded_filename = urllib.parse.unquote(filename)
    base_resolved = BASE_DIR.resolve()
    full_path = (base_resolved / decoded_filename).resolve()

    if not full_path.exists() or not str(full_path).startswith(str(base_resolved)):
        raise HTTPException(status_code=404, detail="Файл не найден")

    # Определяем MIME-тип по расширению
    is_docx = full_path.suffix.lower() == ".docx"
    media_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' if is_docx else 'message/rfc822'
    
    download_name = pretty_name or full_path.name
    
    return FileResponse(
        path=str(full_path), 
        media_type=media_type, 
        filename=download_name
    )

@app.get("/")
async def read_index(): return FileResponse(static_dir / "index.html")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
