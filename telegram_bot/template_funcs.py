from jinja2 import Template


def read_template(file: str) -> str:
    with open(file, 'r') as f:
        return f.read()


def output_text(template_file: str, render: dict) -> str:
    body = read_template(template_file)
    return Template(body).render(render)
