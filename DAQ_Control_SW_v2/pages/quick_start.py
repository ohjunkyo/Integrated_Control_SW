"""Quick Start tab, mirroring ui_manager._create_quick_start_tab.

The guide text is read from the Tk source (ast, no Tk import) so the two apps
never drift apart. Emoji are replaced by words or dropped (v2 uses none).
"""
import ast
import os
import re

from PyQt5.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

from core import env

WORDS = {"\u2500\u25b6": "\u2500>", "\U0001f7e2": "green", "\U0001f534": "red", "✅": "Done", "❌": "Failed"}
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u23e9-\u23ff\u25b6]+ ?")   # emoji + its trailing space


def load_guide():
    src = os.path.join(env.DAQ_DIR, "ui_manager.py")
    try:
        tree = ast.parse(open(src, encoding="utf-8").read())
    except (OSError, SyntaxError) as e:
        return f"Could not read the guide from {src}: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_create_quick_start_tab":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and any(getattr(t, "id", "") == "guide" for t in sub.targets):
                    text = ast.literal_eval(sub.value)
                    for k, v in WORDS.items():
                        text = text.replace(k, v)
                    return EMOJI.sub("", text)
    return "Quick start guide not found in the Tk app."


class QuickStartPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(load_guide())
        lay.addWidget(txt)
