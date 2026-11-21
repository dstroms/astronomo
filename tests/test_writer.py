import numpy as np
from matplotlib.figure import Figure

from writer import LogWriter, md_cell, md_text

HOSTILE = "50% of $x_1$ | pipe\nnew line & #hash {braces} back\\slash \u00b1 5 \u00b0 caf\u00e9 \u4e2d"


def test_md_cell_keeps_a_table_row_on_one_line():
    out = md_cell("a | b\nc\\d")
    assert "\n" not in out and out == "a \\| b c\\\\d"
    assert md_cell(None) == "--" and md_cell("") == "--"


def test_md_text_collapses_whitespace():
    assert md_text("a\n\n  b \t c") == "a b c" and md_text(None) == ""


def _doc(tmp_path):
    fig = Figure(figsize=(3, 2))
    ax = fig.add_subplot()
    ax.imshow(np.random.default_rng(0).random((20, 30)))
    img = tmp_path / "f.png"
    fig.savefig(img)
    return {
        "title": "Mission log: T & the _title_",
        "abstract": HOSTILE,
        "keywords": ["a_b", "c&d"],
        "sections": [
            {"heading": "Summary", "paragraphs": [HOSTILE], "tables": [], "figures": []},
            {
                "heading": "Observations",
                "paragraphs": ["ok"],
                "tables": [{"caption": HOSTILE, "columns": ["a_b", "c%"], "rows": [[HOSTILE, "1"], ["--"]]}],
                "figures": [{"path": str(img), "caption": "First frame"}, {"path": str(tmp_path / "missing.png"), "caption": "x"}],
            },
            {"heading": "Errors and notes", "paragraphs": ["none"], "tables": [], "figures": []},
        ],
    }


META = {"mission_id": "m1", "plugin_name": "demo_plugin", "simulated": True, "site": {"name": "S", "lat": 1.0, "lon": 2.0},
        "observations": [{"target": {"name": "A_B", "ra_deg": 1.0, "dec_deg": 2.0}, "weather": {"cloud_cover_pct": 5.0, "source": "x"},
                          "exposure_plan": {"count": 2, "exposure_s": 10, "gain": 80, "filter": "IRCUT"}}]}


def test_writer_renders_a_markdown_log_with_figures(settings, tmp_path):
    res = LogWriter(settings).write(_doc(tmp_path), META, tmp_path / "log", basename="mission_log_2026-10-21")

    assert res.path.endswith("mission_log_2026-10-21.md") and res.title.startswith("Mission log")
    assert any("missing.png" in n for n in res.notes)  # a missing figure is skipped, not fatal
    text = (tmp_path / "log" / "mission_log_2026-10-21.md").read_text()
    assert text.startswith("# Mission log: T & the _title_")
    assert "> **Simulation.**" in text
    headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert headings == ["## Summary", "## Observations", "## Observing log", "## Errors and notes", "## Reproducibility"]
    assert "![First frame](figures/fig01_f.png)" in text and "missing.png" not in text
    assert (tmp_path / "log" / "figures" / "fig01_f.png").is_file()
    assert not list((tmp_path / "log").glob("*.pdf")) and not list((tmp_path / "log").glob("*.tex"))


def test_hostile_text_cannot_break_a_table(settings, tmp_path):
    LogWriter(settings).write(_doc(tmp_path), META, tmp_path / "log")
    text = (tmp_path / "log" / "mission_log.md").read_text()
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "pipe" in ln]
    assert len(rows) == 1  # the newline and the pipe in the cell did not split or widen the row
    assert "\\|" in rows[0] and rows[0].endswith(" | 1 |")  # the pipe is escaped; the row still has two cells


def test_observing_log_goes_after_the_observations_and_reproducibility_is_last(settings, tmp_path):
    LogWriter(settings).write(_doc(tmp_path), {**META, "simulated": False}, tmp_path / "log")
    text = (tmp_path / "log" / "mission_log.md").read_text()
    assert "Simulation." not in text
    assert text.index("## Observations") < text.index("## Observing log") < text.index("## Reproducibility")
    assert "| Mission ID | m1 |" in text and "| Language model | none (all text is deterministic) |" in text
    assert "A_B" in text


def test_a_doc_with_no_observations_section_gets_the_observing_log_appended(settings, tmp_path):
    doc = {"title": "T", "abstract": "", "keywords": [], "sections": [{"heading": "Summary", "paragraphs": ["p"], "tables": [], "figures": []}]}
    LogWriter(settings).write(doc, {**META, "observations": []}, tmp_path / "log")
    text = (tmp_path / "log" / "mission_log.md").read_text()
    assert text.index("## Summary") < text.index("## Observing log") < text.index("## Reproducibility")
