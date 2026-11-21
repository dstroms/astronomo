"""The roadmap is the master document: every Markdown file is listed in it, and no link in the docs is broken.

A new document without a row in the roadmap's documentation map fails here, so nothing is orphaned.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".venv", "node_modules", "data", "__pycache__", ".git", ".pytest_cache", ".agents"}
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _markdown_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.md"):
        rel = p.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts) or rel.parts[0] == "tests":
            continue
        out.append(p)
    return sorted(out)


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, drop punctuation, spaces become hyphens."""
    text = re.sub(r"`", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    out = set()
    in_code = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if not in_code:
            m = re.match(r"#{1,6}\s+(.*)", line)
            if m:
                out.add(_slug(m.group(1)))
    return out


def _links(path: Path):
    text = path.read_text()
    text = re.sub(r"```.*?```", "", text, flags=re.S)  # a command or example in a code block is not a link
    text = re.sub(r"`[^`\n]*`", lambda m: m.group(0).replace("[", " ").replace("]", " "), text)
    return _LINK.findall(text)


def test_there_are_documents_to_check():
    names = {p.relative_to(ROOT).as_posix() for p in _markdown_files()}
    assert {"README.md", "ROADMAP.md", "CONTRIBUTING.md"} <= names
    assert any(n.startswith("docs/first-light/") for n in names)


def test_every_document_is_listed_in_the_roadmap():
    roadmap = (ROOT / "ROADMAP.md").read_text()
    missing = [
        p.relative_to(ROOT).as_posix()
        for p in _markdown_files()
        if p.name != "ROADMAP.md" and p.relative_to(ROOT).as_posix() not in roadmap
    ]
    assert not missing, f"not in the roadmap's documentation map (add a row): {missing}"


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(path):
    broken = []
    for target in _links(path):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.I):  # http:, https:, mailto:
            continue
        file_part, _, anchor = target.partition("#")
        dest = path if not file_part else (path.parent / file_part).resolve()
        if not dest.exists():
            broken.append(target)
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            broken.append(target)
    assert not broken, f"{path.relative_to(ROOT)} has broken links: {broken}"


def test_every_roadmap_phase_has_exit_criteria():
    text = (ROOT / "ROADMAP.md").read_text()
    phases = re.split(r"^### (?=Phase \d+: )", text, flags=re.M)[1:]
    assert len(phases) == 8
    for block in phases:
        title = block.splitlines()[0]
        block = re.split(r"^## ", block, flags=re.M)[0]
        assert "**Exit criteria" in block, f"{title} has no exit criteria"


def test_issue_forms_are_valid_and_the_private_report_link_is_there():
    yaml = pytest.importorskip("yaml")
    folder = ROOT / ".github" / "ISSUE_TEMPLATE"
    config = yaml.safe_load((folder / "config.yml").read_text())
    assert config["blank_issues_enabled"] is False
    assert any("security/advisories/new" in c["url"] for c in config["contact_links"])
    forms = sorted(p for p in folder.glob("*.yml") if p.name != "config.yml")
    assert len(forms) >= 3
    for form in forms:
        doc = yaml.safe_load(form.read_text())
        assert doc["name"] and doc["description"] and doc["body"], form.name
        ids = [b["id"] for b in doc["body"] if "id" in b]
        assert len(ids) == len(set(ids)), f"{form.name} repeats an id"
        for block in doc["body"]:
            assert block["type"] in {"markdown", "input", "textarea", "dropdown", "checkboxes"}
            if block["type"] == "dropdown":
                assert len(block["attributes"]["options"]) >= 2
