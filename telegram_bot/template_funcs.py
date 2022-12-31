from jinja2 import Template


def read_template(file) -> str:
    txt_file = open(file, 'r')
    txt = txt_file.read()
    txt_file.close()
    return txt


def output_text(template_file, render):
    file_text = read_template(template_file)
    body = u'{}'.format(file_text)
    now_text = Template(body)
    result = now_text.render(render)
    return result
