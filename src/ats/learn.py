"""Learn from company sites while YOU fill them.

* SiteRecorder: a listener injected into the company-site tab records every field you change
  (question label -> your answer). It survives page navigations inside that tab. When you finish,
  the answers are saved to the answer memory, so the same questions fill automatically on the next
  site (any site, not just this one).
* SiteKnowledge: per-domain notes in data/site_knowledge.json — visits, applications, whether it needs
  an account, which "Apply" button worked — shown as a hint the next time you land there.
"""
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from rich.table import Table

from src.config import DATA_DIR
from src.core.review import console

RECORD_JS = r"""
(() => {
  if (window.__jbRecorder) return; window.__jbRecorder = true;
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    const root = el.getRootNode();
    if (el.id) { const l = root.querySelector && root.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && txt(l.innerText)) return txt(l.innerText); }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(e => e.innerText).join(' '); if (txt(t)) return txt(t); }
    if (el.getAttribute('aria-label')) return txt(el.getAttribute('aria-label'));
    const w = el.closest('label'); if (w && txt(w.innerText)) return txt(w.innerText);
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return txt(fs.querySelector('legend').innerText);
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = p.querySelector('label, legend, [class*="label" i], [class*="question" i]');
      if (l && !l.contains(el) && txt(l.innerText)) return txt(l.innerText);
    }
    return txt(el.getAttribute('placeholder') || el.name || '');
  };
  const send = el => {
    const type = (el.type || '').toLowerCase();
    if (['password', 'hidden', 'file', 'submit', 'button', 'search'].includes(type)) return;
    let value = '', kind = 'text', options = [], label = labelOf(el);
    if (el.tagName === 'SELECT') { value = txt(el.selectedOptions[0]?.text); kind = 'select';
      options = [...el.options].map(o => txt(o.text)).filter(Boolean); }
    else if (type === 'radio') { if (!el.checked) return; kind = 'radio';
      const fs = el.closest('fieldset'); const g = fs && fs.querySelector('legend');
      value = labelOf(el); label = g ? txt(g.innerText) : (el.name || label); }
    else if (type === 'checkbox') { value = el.checked ? 'Yes' : 'No'; kind = 'single_checkbox'; }
    else { value = el.value; kind = el.tagName === 'TEXTAREA' ? 'textarea' : 'text'; }
    if (!label || !txt(value) || value.length > 2000) return;
    try { window.jbLearn({label, value: txt(value), kind, options}); } catch (e) {}
  };
  document.addEventListener('change', e => { if (e.target && e.target.matches && e.target.matches('input,select,textarea')) send(e.target); }, true);
  document.addEventListener('focusout', e => { if (e.target && e.target.matches && e.target.matches('input,textarea')) send(e.target); }, true);
})();
"""
SKIP_LABEL = re.compile(r"password|captcha|otp|verification code|search|card number|cvv|ssn|aadhaar|pan\b", re.I)


class SiteRecorder:
    """Records label -> value for fields the user fills in this tab."""

    def __init__(self, page):
        self.page = page
        self.answers: dict[str, dict] = {}
        try:
            page.expose_binding("jbLearn", lambda source, data: self._got(data))
        except PlaywrightError:
            pass                                  # already exposed in this tab
        page.add_init_script(RECORD_JS)           # every future navigation in this tab
        try:
            page.evaluate(RECORD_JS)              # the page that is open right now
        except PlaywrightError:
            pass

    def _got(self, data: dict):
        label = re.sub(r"\s*\*\s*$|\s*\(required\)\s*$", "", data.get("label", "")).strip()
        if label and not SKIP_LABEL.search(label):
            self.answers[label] = data

    def save_to_memory(self, engine, domain: str, filled: dict[str, str]) -> int:
        """Show what was learned — answers you typed, or tool answers you corrected — save on OK."""
        new = {k: v for k, v in self.answers.items() if filled.get(k, "").strip() != v["value"].strip()}
        if not new:
            return 0
        table = Table(title=f"Learned from {domain} — answers you typed")
        table.add_column("Question")
        table.add_column("Your answer")
        for k, v in new.items():
            table.add_row(k[:70], v["value"][:60])
        console.print(table)
        raw = console.input("Save these for future applications? [Y/n] > ").strip().lower()
        if raw.startswith("n"):
            return 0
        from src.core.models import Field
        for k, v in new.items():
            engine.remember(Field(k, v.get("kind", "text"), v.get("options") or []), v["value"],
                            source=f"learned:{domain}")
        return len(new)


class SiteKnowledge:
    PATH = DATA_DIR / "site_knowledge.json"

    def __init__(self):
        self.data = json.loads(self.PATH.read_text()) if self.PATH.exists() else {}

    @staticmethod
    def domain(url: str) -> str:
        host = urlsplit(url).hostname or ""
        return re.sub(r"^www\.", "", host)

    def get(self, url: str) -> dict:
        return self.data.get(self.domain(url), {})

    def update(self, url: str, **kw):
        d = self.data.setdefault(self.domain(url), {"visits": 0, "applied": 0})
        for k, v in kw.items():
            if k in ("visits", "applied"):
                d[k] = d.get(k, 0) + v
            elif v not in (None, ""):
                d[k] = v
        d["last_seen"] = datetime.now().isoformat(timespec="seconds")
        self.PATH.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))

    def hint(self, url: str) -> str:
        d = self.get(url)
        if not d:
            return ""
        parts = [f"seen {d.get('visits', 0)}x", f"applied {d.get('applied', 0)}x"]
        if d.get("needs_account"):
            parts.append("needs an account/login")
        if d.get("apply_button"):
            parts.append(f"apply button: '{d['apply_button']}'")
        if d.get("note"):
            parts.append(d["note"])
        return " · ".join(parts)
