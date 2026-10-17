from pathlib import Path

from pypdf import PdfReader

from src.config import DATA_DIR


def load_resume_text(path: Path) -> str:
    """PDF -> text, cached in data/resume.txt until the PDF changes."""
    if not path.exists():
        print(f"[warn] Resume not found at {path} — LLM answers will use profile.yaml only.")
        return ""
    cache = DATA_DIR / "resume.txt"
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        return cache.read_text()
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
    cache.write_text(text)
    return text
