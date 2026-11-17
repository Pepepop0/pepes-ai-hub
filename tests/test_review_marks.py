"""Marks painted in a lab reviewer, drawn back onto the rated picture."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "review_marks.py"
spec = importlib.util.spec_from_file_location("review_marks_under_test", SCRIPT)
marks = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = marks
spec.loader.exec_module(marks)


def page(tmp_path: Path) -> Path:
    folder = tmp_path / "page"
    (folder / "Q6").mkdir(parents=True)
    Image.new("RGB", (400, 200), (90, 90, 90)).save(folder / "Q6" / "D2_final.png")
    data = [
        {
            "id": "Q6",
            "variants": [{"id": "D2", "paths": {"final": "Q6/D2_final.png", "raw": "x.png"}}],
        }
    ]
    html = "<script>\nconst DATA = " + json.dumps(data) + ";\nconst KEY = 'k';\n</script>"
    (folder / "avaliar-composicao.html").write_text(html, encoding="utf-8")
    return folder


def test_each_failure_stroke_lands_where_it_was_painted_and_successes_stay_apart(tmp_path):
    rated = {
        "Q6": {
            "D2": {
                "r": "P",
                "m": [
                    {"k": "f", "r": 0.02, "p": [[0.75, 0.25], [0.8, 0.3]]},
                    {"k": "g", "r": 0.02, "p": [[0.1, 0.8]]},
                ],
            },
            "D3": {"r": "S"},
        }
    }
    review = tmp_path / "avaliacao.md"
    review.write_text("| tabela |\n\n```json\n" + json.dumps(rated) + "\n```\n", encoding="utf-8")
    lines = marks.run(review, page(tmp_path), tmp_path / "out")
    assert lines == ["Q6 D2: 1 falha(s), 1 acerto(s); falhas em x 73-82%, y 21-34%"]
    failure = Image.open(tmp_path / "out" / "Q6_D2_falha.png")
    assert failure.getpixel((310, 55)) == 255, "the painted failure"
    assert failure.getpixel((40, 160)) == 0, "a success is not a failure"
    marked = Image.open(tmp_path / "out" / "Q6_D2_marcas.png").convert("RGB")
    red, green, _ = marked.getpixel((310, 55))
    assert red > green, "the failure is drawn red over the picture"
