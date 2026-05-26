from docxtpl import DocxTemplate

def generate_docx(template_path, output_path, context):
    doc = DocxTemplate(template_path)
    doc.render(context)
    doc.save(output_path)
