from docxtpl import DocxTemplate

def generate_docx(template_path, output_path, context):
    """Рендеринг docx шаблона с использованием Jinja2-подобного синтаксиса."""
    doc = DocxTemplate(template_path)
    doc.render(context)
    doc.save(output_path)
