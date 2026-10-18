"""Generic HTML form filler shared by LinkedIn Easy Apply, Greenhouse, Lever and unknown sites.

1. scan(): one JS pass over a form root finds every visible control, works out its question
   label, options, required flag and current value, and tags it with data-jb="<id>".
2. fill(): for each control -> keep prefilled value, or resolve an answer through the session
   (profile -> cache -> LLM -> you) and set it.
3. review_and_submit(): show every field + value, let you edit, then click Submit and verify.
"""
import difflib
import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout
from rich.table import Table

from src.core.models import Field, QuitRun, SkipJob
from src.core.review import console
from src.core.session import ApplySession

SCAN_JS = r"""
(root) => {
  const doc = root.getRootNode();   // Document, or ShadowRoot (LinkedIn renders Easy Apply in one)
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const PLACEHOLDER = /^(select|choose|please select|--)/i;

  const labelOf = el => {
    if (el.id) {
      const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && txt(l.innerText)) return txt(l.innerText);
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(i => doc.getElementById(i)).filter(Boolean).map(e => e.innerText).join(' ');
      if (txt(t)) return txt(t);
    }
    const al = el.getAttribute('aria-label');
    if (al) return txt(al);
    const wrap = el.closest('label');
    if (wrap && txt(wrap.innerText)) return txt(wrap.innerText);
    const named = el.parentElement && el.parentElement.closest('[aria-label]');   // LinkedIn: <div aria-label=..><input>
    if (named && named !== root && root.contains(named)) return txt(named.getAttribute('aria-label'));
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = p.querySelector('label, legend');
      if (l && !l.contains(el) && txt(l.innerText)) return txt(l.innerText);
    }
    return txt(el.getAttribute('placeholder') || el.name || '');
  };

  const groupLabel = members => {
    const fs = members[0].closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg && txt(lg.innerText)) return txt(lg.innerText);
    }
    const rg = members[0].closest('[role=radiogroup], [role=group]');
    if (rg && (rg.getAttribute('aria-labelledby') || rg.getAttribute('aria-label'))) return labelOf(rg);
    let p = members[0].parentElement;
    while (p && !members.every(m => p.contains(m))) p = p.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      const cand = [...p.querySelectorAll('label, legend, [class*="label"], [class*="question"]')]
        .find(l => !members.some(m => l.contains(m) || (m.id && l.htmlFor === m.id)) && txt(l.innerText));
      if (cand) return txt(cand.innerText);
    }
    return members[0].name || '';
  };

  const isReq = el => el.required || el.getAttribute('aria-required') === 'true';
  const isBad = el => el.getAttribute('aria-invalid') === 'true';
  const out = [];
  const done = new Set();
  const node = {};          // id -> element, to sort controls in page order
  let n = 0;

  for (const el of root.querySelectorAll('input, select, textarea')) {
    if (done.has(el) || el.disabled) continue;
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    const id = 'jb' + (n++);

    if (type === 'radio' || type === 'checkbox') {
      let members = [el];
      if (el.name) members = [...root.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)];
      else if (el.closest('fieldset')) members = [...el.closest('fieldset').querySelectorAll(`input[type="${type}"]`)];
      members.forEach(m => done.add(m));
      const lbl = m => m.id && doc.querySelector(`label[for="${CSS.escape(m.id)}"]`);
      if (!members.some(m => vis(m) || (lbl(m) && vis(lbl(m))) || (m.closest('label') && vis(m.closest('label'))))) continue;
      node[id] = members[0];
      if (type === 'checkbox' && members.length === 1) {
        el.setAttribute('data-jb', id);
        out.push({id, kind: 'single_checkbox', label: labelOf(el), options: ['Yes', 'No'],
                  required: isReq(el), current: el.checked ? 'Yes' : '', invalid: isBad(el), combobox: false});
        continue;
      }
      members.forEach((m, i) => m.setAttribute('data-jb', `${id}-${i}`));
      out.push({id, kind: type, label: groupLabel(members), options: members.map(labelOf),
                required: members.some(isReq), current: members.filter(m => m.checked).map(labelOf).join(', '),
                invalid: members.some(isBad), combobox: false});
      continue;
    }

    if (type !== 'file' && !vis(el)) continue;
    node[id] = el;
    el.setAttribute('data-jb', id);
    const kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : type === 'file' ? 'file' : 'text';
    let options = [], current = '';
    if (kind === 'select') {
      options = [...el.options].filter(o => o.value && !PLACEHOLDER.test(txt(o.text))).map(o => txt(o.text));
      const t = txt(el.selectedOptions[0]?.text || '');
      current = el.value && !PLACEHOLDER.test(t) ? t : '';
    } else if (kind === 'file') {
      current = el.files && el.files.length ? el.files[0].name : '';
    } else {
      current = el.value || '';
    }
    out.push({id, kind, label: labelOf(el), options, required: isReq(el), current, invalid: isBad(el),
              combobox: el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list'
                        || el.getAttribute('data-automation-id') === 'searchBox'});
  }
  // custom dropdowns (Workday): <button aria-haspopup="listbox">Select One</button>
  for (const el of root.querySelectorAll('button[aria-haspopup="listbox"]')) {
    if (el.disabled || !vis(el)) continue;
    const id = 'jb' + (n++);
    node[id] = el;
    el.setAttribute('data-jb', id);
    const t = txt(el.innerText || el.getAttribute('value'));
    out.push({id, kind: 'listbox', label: labelOf(el), options: [], required: isReq(el),
              current: !t || PLACEHOLDER.test(t) ? '' : t, invalid: isBad(el), combobox: false});
  }
  out.sort((a, b) => node[a.id].compareDocumentPosition(node[b.id]) & Node.DOCUMENT_POSITION_PRECEDING ? 1 : -1);
  return out;
}
"""

YES = {"yes", "y", "true", "agree", "i agree", "checked", "1"}


@dataclass
class Control:
    id: str
    field: Field
    current: str
    invalid: bool
    combobox: bool
    value: str = ""
    source: str = ""          # prefilled | profile/cache/llm/user | file | skipped


def _clean_label(label: str) -> str:
    label = re.sub(r"\s*Select One\b", "", label.strip(), flags=re.I)
    label = re.sub(r"\s*\*\s*$|\s*\(required\)\s*$|\s*Required\s*$", "", label, flags=re.I).strip()
    half = len(label) // 2
    if len(label) > 10 and label[:half].strip() == label[half:].strip():  # LinkedIn repeats labels
        label = label[:half].strip()
    return label


def _closest(value: str, options: list[str]) -> str | None:
    for opt in options:
        if opt.lower() == value.lower():
            return opt
    m = difflib.get_close_matches(value, options, n=1, cutoff=0.5)
    return m[0] if m else None


class FormFiller:
    def __init__(self, session: ApplySession, upload: str = "always", all_required: bool = False):
        """upload: 'always' = attach resume to empty resume inputs; 'on_error' = only when the site
        complains (LinkedIn already keeps your resume)."""
        self.s = session
        self.upload = upload
        self.all_required = all_required     # LinkedIn often doesn't mark required questions in the DOM

    # --- scan ----------------------------------------------------------------

    def scan(self, root) -> list[Control]:
        raw = root.evaluate(SCAN_JS)
        controls = []
        for r in raw:
            label = _clean_label(r["label"]) or "(unlabelled field)"
            required = ((self.all_required and r["kind"] != "single_checkbox") or r["required"] or "*" in r["label"]
                        or "required" in r["label"].lower())
            f = Field(label, r["kind"], [o for o in r["options"] if o], required)
            c = Control(r["id"], f, r["current"], r["invalid"], r["combobox"])
            if (c.combobox or f.kind == "listbox") and not f.options and not c.current:
                f.options = self._combobox_options(root, c)
            controls.append(c)
        return controls

    def _combobox_options(self, root, c: Control) -> list[str]:
        """React-select style dropdowns only reveal options when opened."""
        loc = self._loc(root, c)
        try:
            loc.click(timeout=2000)
            root.page.wait_for_timeout(500)
            root.page.wait_for_timeout(300)
            opts = root.page.locator("[role='option']:visible")      # popups are often attached to <body>
            texts = [t.strip() for t in opts.all_inner_texts()[:80] if t.strip()]
            loc.press("Escape")
            return texts
        except PlaywrightError:
            return []

    # --- fill ----------------------------------------------------------------

    def fill(self, root, force_invalid: bool = False) -> list[Control]:
        controls = self.scan(root)
        file_seen = False
        for c in controls:
            f = c.field
            if f.kind == "file":
                self._file(root, c, first=not file_seen, force=force_invalid and c.invalid)
                file_seen = True
                continue
            redo = force_invalid and c.invalid
            if c.current and not redo:
                mine = self.s.engine.profile_answer(f)       # profile.yaml beats a site default
                if mine and mine.value != c.current:
                    self.set(root, c, mine.value)
                    c.source = "profile"
                else:
                    c.value, c.source = c.current, "prefilled"
                continue
            if not f.required and not redo:
                value = self.s.answer(f, optional=True)       # only fill optional fields we already know
                if value is None:
                    c.source = "skipped"
                    continue
            else:
                value = self.s.answer(f, force_ask=redo)
            for _ in range(2):          # answer must be one of the options
                if not (f.options and f.kind in ("radio", "select", "listbox")) or _closest(value, f.options):
                    break
                console.print(f"  [yellow]'{value}' is not one of the options[/]")
                value = self.s.answer(f, force_ask=True)
            self.set(root, c, value)
            if c.source != "FAILED":
                c.source = self.s.last_source
            self.s.human.pause()
        return controls

    def _file(self, root, c: Control, first: bool, force: bool):
        label = c.field.label.lower()
        is_resume = bool(re.search(r"resume|cv\b|curriculum", label)) or (first and "cover" not in label)
        if not is_resume:
            c.source = "skipped (cover letter)"
            return
        need = force or (self.upload == "always" and not c.current) or (self.upload == "on_error" and c.invalid)
        if c.current and not force:
            c.value, c.source = c.current, "prefilled"
        if need:
            self._loc(root, c).set_input_files(str(self.s.resume_path))
            c.value, c.source = self.s.resume_path.name, "file"
            root.page.wait_for_timeout(1500)

    def _loc(self, root, c: Control, idx: int | None = None):
        sel = f'[data-jb="{c.id}"]' if idx is None else f'[data-jb="{c.id}-{idx}"]'
        return root.locator(sel).first

    def set(self, root, c: Control, value: str):
        f = c.field
        try:
            if f.kind in ("text", "textarea"):
                loc = self._loc(root, c)
                loc.click()
                loc.fill("")
                if c.combobox:
                    loc.press_sequentially(value, delay=60)
                    loc.page.wait_for_timeout(1200)
                    opts = loc.page.locator("[role='option']:visible, [data-automation-id='promptOption']:visible")
                    if not opts.count():
                        loc.press("Enter")                     # Workday searches on Enter
                        loc.page.wait_for_timeout(1200)
                    match = opts.filter(has_text=value)
                    if match.count() or opts.count():
                        (match if match.count() else opts).first.click()
                    else:
                        loc.press("ArrowDown")
                        loc.press("Enter")
                else:
                    loc.fill(value)
            elif f.kind == "select":
                loc = self._loc(root, c)
                opt = _closest(value, f.options) or value
                loc.select_option(label=opt)
                value = opt
            elif f.kind == "radio":
                opt = _closest(value, f.options)
                if opt is None:
                    raise ValueError(f"'{value}' is not one of {f.options}")
                self._check(self._loc(root, c, f.options.index(opt)), True)
                value = opt
            elif f.kind == "checkbox":
                picked = [o for v in value.split(",") if (o := _closest(v.strip(), f.options))]
                for i, opt in enumerate(f.options):
                    self._check(self._loc(root, c, i), opt in picked)
                value = ", ".join(picked)
            elif f.kind == "listbox":
                loc = self._loc(root, c)
                loc.click()
                root.page.wait_for_timeout(600)
                opt = _closest(value, f.options) or value
                option = root.page.get_by_role("option", name=opt, exact=True).filter(visible=True)
                if not option.count():
                    option = root.page.locator("[role='option']:visible").filter(has_text=opt)
                option.first.click(timeout=3000)
                value = opt
            elif f.kind == "single_checkbox":
                self._check(self._loc(root, c), value.strip().lower() in YES)
            c.value, c.source = value, c.source if c.source not in ("", "skipped") else "answer"
        except (PlaywrightError, ValueError) as e:
            if f.kind == "listbox":
                root.page.keyboard.press("Escape")
            console.print(f"  [red]could not set[/] {f.label!r} → {value!r}: {str(e).splitlines()[0]}")
            c.value, c.source = value, "FAILED"

    def _check(self, loc, want: bool):
        if loc.is_checked() == want:
            return
        try:
            loc.check(force=True, timeout=2000) if want else loc.uncheck(force=True, timeout=2000)
        except PlaywrightError:
            loc.evaluate("e => e.click()")

    # --- review + submit -------------------------------------------------------

    def summary(self, controls: list[Control], title: str):
        table = Table(title=title, show_lines=False)
        table.add_column("#", style="dim")
        table.add_column("Field")
        table.add_column("Value")
        table.add_column("From", style="dim")
        for i, c in enumerate(controls, 1):
            if c.source == "skipped" and not c.field.required:
                continue
            style = "red" if c.source == "FAILED" or (c.field.required and not c.value) else ""
            table.add_row(str(i), c.field.label[:70], f"[{style}]{c.value or '—'}[/]" if style else (c.value or "—"),
                          c.source)
        console.print(table)

    def review_edits(self, root, controls: list[Control], title: str, action: str = "SUBMIT",
                     editable: bool = True, extra: str = "") -> str:
        """Show the table, let the user edit fields by number. Returns '' (Enter) or an extra letter."""
        while True:
            self.summary(controls, title)
            hint = "number = edit field  " if editable else ""
            raw = console.input(f"[bold cyan][Enter][/] {action}  {hint}{extra}s skip  q quit > ").strip().lower()
            if raw == "q":
                raise QuitRun()
            if raw == "s":
                raise SkipJob()
            if not raw:
                return ""
            if raw.isalpha() and len(raw) == 1 and raw in extra.lower():
                return raw
            if editable and raw.isdigit() and 1 <= int(raw) <= len(controls):
                c = controls[int(raw) - 1]
                opts = f" options: {c.field.options}" if c.field.options else ""
                new = console.input(f"New value for {c.field.label!r}{opts} > ").strip()
                if new:
                    if new.isdigit() and c.field.options and 1 <= int(new) <= len(c.field.options):
                        new = c.field.options[int(new) - 1]
                    self.set(root, c, new)
                    c.source = "user"
                    self.s.engine.remember(c.field, new)

    def review_and_submit(self, root, controls: list[Control], submit, success, title: str,
                          editable: bool = True) -> str:
        """Show everything, allow edits, click submit, verify. Returns applied | failed."""
        for attempt in range(3):
            self.review_edits(root, controls, title, "SUBMIT", editable)
            self.s.human.pause()
            submit.click()
            outcome = self._wait_outcome(root, success)
            if outcome == "applied":
                return "applied"
            if outcome == "captcha":
                console.input("[yellow]Captcha shown — solve it in the browser, then press Enter[/] > ")
                if self._wait_outcome(root, success, seconds=10) == "applied":
                    return "applied"
            if outcome == "errors":
                console.print("[yellow]Site rejected some fields — re-asking those.[/]")
                controls = self.fill(root, force_invalid=True)
                continue
            break
        ans = console.input("Could not confirm submission. Did it go through? [y/N] > ").strip().lower()
        return "applied" if ans == "y" else "failed"

    def _wait_outcome(self, root, success, seconds: int = 15) -> str:
        page = root.page
        for _ in range(seconds * 2):
            try:
                if success():
                    return "applied"
                if page.locator("iframe[src*='hcaptcha'], iframe[src*='recaptcha/api2/bframe']").filter(
                        visible=True).count():
                    return "captcha"
                if root.locator("[aria-invalid='true']:visible, .error:visible, .field-error:visible").count():
                    return "errors"
            except PWTimeout:
                pass
            except PlaywrightError:
                return "applied" if success() else "unknown"   # page navigated away
            page.wait_for_timeout(500)
        return "unknown"
