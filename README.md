# Job Apply Assistant

Searches jobs, scores them against your resume, fills applications — **you approve each one**.
Sites: Naukri (done), LinkedIn Easy Apply (next).

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
```

## How answers are chosen
1. `profile.yaml → answers` (all key words present in the question)
2. `data/answers_cache.json` — answers you approved earlier
3. LLM (Gemini / OpenAI) using your profile + resume
4. Nothing found → you type it

Profile/cache answers fill automatically (`review.auto_accept_known`); LLM answers are always shown to you first.

## When something breaks
Sites change markup. Screenshots + HTML are saved to `data/debug/`. Update the `SEL` dict in `src/sites/naukri.py`.

## Layout
```
src/core/   browser, human pacing, review, answer engine, llm, matcher, tracker
src/sites/  base.py (adapter contract), naukri.py, linkedin.py (next)
src/runner.py  orchestration     src/main.py  CLI
```
