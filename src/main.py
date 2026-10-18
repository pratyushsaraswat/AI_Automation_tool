import argparse

from src import runner
from src.config import load_settings


def main():
    p = argparse.ArgumentParser(prog="jobbot", description="Assisted job applications (you approve every one).")
    sub = p.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="open the browser and log in once")
    lg.add_argument("--site", choices=list(runner.ADAPTERS), default="naukri")

    r = sub.add_parser("run", help="search, score and apply")
    r.add_argument("--site", choices=list(runner.ADAPTERS), default="naukri")
    r.add_argument("--limit", type=int, help="max applications this run")
    r.add_argument("--dry-run", action="store_true", help="search + score only, never click Apply")

    ap = sub.add_parser("apply", help="apply to job links you give (Workday, Greenhouse, Lever, any site)")
    ap.add_argument("--url", nargs="+", default=[], help="one or more job links")
    ap.add_argument("--file", help="text file with one job link per line")

    ou = sub.add_parser("outreach", help="message people at a company on LinkedIn for a referral")
    ou.add_argument("--company", help="company name (omit with --pending)")
    ou.add_argument("--role", default="", help="role you applied for")
    ou.add_argument("--job-url", default="", help="job link (used to tailor the message to its skills)")
    ou.add_argument("--pending", type=int, metavar="DAYS", help="all applied jobs in last DAYS with no outreach yet")
    ou.add_argument("--person", nargs="+", default=[], help="LinkedIn profile link(s) to contact directly")

    an = sub.add_parser("answers", help="view / teach / clean saved answers (the tool's memory)")
    an_sub = an.add_subparsers(dest="action", required=True)
    al = an_sub.add_parser("list", help="show saved answers")
    al.add_argument("search", nargs="?", default="", help="filter text, e.g. ctc")
    ast = an_sub.add_parser("set", help='save an answer: answers set "Notice period?" "30"')
    ast.add_argument("question")
    ast.add_argument("answer")
    ad = an_sub.add_parser("delete", help="delete answer by number from `answers list`")
    ad.add_argument("number", type=int)
    an_sub.add_parser("learn", help="answer common questions once, up front")

    sub.add_parser("report", help="show application history")

    ex = sub.add_parser("export", help="write data/applications.csv (auto-updated after every job too)")
    ex.add_argument("--applied-only", action="store_true", help="only companies actually applied to")

    args = p.parse_args()
    if args.cmd == "report":
        runner.report()
        return
    if args.cmd == "answers":
        from src import answers_cli
        {"list": lambda: answers_cli.list_answers(args.search),
         "set": lambda: answers_cli.set_answer(args.question, args.answer),
         "delete": lambda: answers_cli.delete_answer(args.number),
         "learn": answers_cli.learn}[args.action]()
        return
    if args.cmd == "export":
        runner.export(args.applied_only)
        return
    settings = load_settings()
    if args.cmd == "outreach":
        if not args.company and args.pending is None:
            p.error("give --company NAME [--role R --job-url URL] or --pending DAYS")
        runner.outreach(settings, args.company, args.role, args.pending, args.job_url, args.person)
    elif args.cmd == "apply":
        urls = list(args.url)
        if args.file:
            urls += [l.strip() for l in open(args.file) if l.strip() and not l.startswith("#")]
        if not urls:
            p.error("give --url LINK [LINK ...] or --file links.txt")
        runner.apply_links(urls, settings)
    elif args.cmd == "login":
        runner.login(args.site, settings)
    else:
        runner.run(args.site, settings, args.limit, args.dry_run)


if __name__ == "__main__":
    main()
