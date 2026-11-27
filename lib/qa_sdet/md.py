"""Text made safe for Markdown: table cells and rows, list lines, paragraphs and <summary> lines."""

import html


def cell(v):
    """Text for one table cell or line: `|` escaped, newlines as <br>, and no raw HTML, images or links (an XSS payload,
    `<!--` or `[text](https://…)` typed into a form must show as text, not hide the report or become a hidden-URL link;
    breaking `](` and `]:` stops inline links, images and reference definitions while `[BLOCKING]` stays readable)."""
    return (
        str(v if v is not None else "")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("<", "&lt;")
        .replace("](", "]\\(")
        .replace("]:", "]\\:")
        .strip()
        .replace("\n", "<br>")
    )


class Raw(str):
    """Already Markdown (a link): row() puts it in as is."""


def one_line(v):
    """Text for a heading or list line: whitespace and newlines collapsed, `|` escaped."""
    return cell(" ".join(str(v if v is not None else "").split()))


def row(*cols):
    return "| " + " | ".join(c if isinstance(c, Raw) else cell(c) for c in cols) + " |"


def paragraph(v):
    """cell() text that stands alone on its line: a leading ``` or # would fence off or head the rest of the report."""
    s = cell(v)
    return ("\\" if s[:1] in tuple("#`~>-+*=_") else "") + s


def summary_text(v):
    """Text for a <details><summary> line: that line is an HTML block, where Markdown escapes (`\\|`) show as typed,
    so it gets HTML escaping only (no tags, entities as text) and one line."""
    return html.escape(" ".join(str(v if v is not None else "").split()), quote=False)
