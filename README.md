# Job Apply Assistant

Searches jobs, scores them against your resume, fills applications — **you approve each one**.
Sites: Naukri, LinkedIn (Easy Apply + external), company sites: Greenhouse & Lever (auto-fill),
any other site (assisted: fills what it can, you submit).

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # fallback browser; uses installed Google Chrome if present
```

1. Put your resume at `resume/resume.pdf`
2. Fill `config/profile.yaml` (true details only)
3. Edit `config/search.yaml` (job titles, cities, experience)
4. Put your key in `config/.env` (`LLM_PROVIDER=gemini` + `GEMINI_API_KEY`, or `openai` + `OPENAI_API_KEY`, or `none`)

## Use
```bash
python -m src.main login --site naukri       # log in once; session saved in data/chrome-profile
python -m src.main run --site naukri --dry-run   # search + score only, no applying
python -m src.main run --site naukri --limit 5   # real run, max 5 applications
python -m src.main report                    # history
python -m src.main login --site linkedin
python -m src.main run --site linkedin --dry-run
python -m src.main run --site linkedin --limit 5
python -m src.main apply --url "https://acme.wd5.myworkdayjobs.com/..."   # any job link (Workday etc.)
python -m src.main apply --file links.txt   # one link per line
python -m src.main export --applied-only     # data/applications.csv (also auto-updated after every job)
```

## External (company-site) applications
LinkedIn "Apply" / Naukri "Apply on company site" open the company site in a new tab, then:
| Site | What happens |
|---|---|
| Greenhouse (incl. embedded iframe) | form filled, full summary, you press Enter to submit |
| Lever | same; solve hCaptcha yourself if shown |
| Workday | Apply → Autofill with Resume → you sign in → each page filled + shown, Enter = Save & Continue → stops at Review, **you submit** |
| Anything else (custom) | visible form filled with known answers; you finish + submit, press `d` |

## Referral outreach (LinkedIn)
After each application the tool offers to message people at that company (recruiters, talent
acquisition, engineering managers). Message is tailored to the job's skills that are on your resume;
`data/prep/<date>-<company>-<role>.md` lists what to revise. You approve every message.
```bash
python -m src.main outreach --pending 7                     # all recent applications, no outreach yet
python -m src.main outreach --company "Atlassian" "Razorpay" "Zepto"     # no job needed: eng leaders + HR
python -m src.main outreach --file companies.txt --role "Backend Engineer"   # one company per line
python -m src.main outreach --company "Acme" --role "Backend Engineer" --job-url "https://..."
```
Settings + message templates: `config/outreach.yaml` (daily_cap 10, per_company 2, mode send|draft).
Log: `data/outreach.csv`.

## CSV
`data/applications.csv`: date, site, company, title, location, status, apply_via
(naukri / linkedin_easy / greenhouse / lever / generic), score, job url, external url.

## How answers are chosen (the tool learns from you)
1. **Answer memory** `data/answer_memory.json` — every answer you type/confirm is stored. Reworded
   questions still match ("Years of Java experience" = "How many years have you worked with Java?").
   Exact match → filled automatically; close match → proposed, you confirm.
2. `profile.yaml → answers` (all key words present in the question)
3. LLM, given your profile + resume + your most similar past answers as examples
4. Nothing found → you type it → saved to memory for next time

```bash
python -m src.main answers learn            # answer ~35 common questions once, up front
python -m src.main answers list [ctc]       # see / search memory
python -m src.main answers set "Notice period?" "30"
python -m src.main answers delete 7
```
Numbers map onto range options automatically (2 → "1-3 years").

## When something breaks
Sites change markup. Screenshots + HTML are saved to `data/debug/`. Update the `SEL` dict in `src/sites/naukri.py`.

## Layout
```
src/core/   browser, human pacing, review, answer engine, llm, matcher, tracker
src/core/forms.py  generic form scanner/filler (LinkedIn modal, Greenhouse, Lever, generic)
src/sites/  base.py (adapter contract), naukri.py, linkedin.py
src/ats/    greenhouse.py, lever.py, generic.py (company sites)
src/runner.py  orchestration     src/main.py  CLI
```
