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

    sub.add_parser("report", help="show application history")

    args = p.parse_args()
    if args.cmd == "report":
        runner.report()
        return
    settings = load_settings()
    if args.cmd == "login":
        runner.login(args.site, settings)
    else:
        runner.run(args.site, settings, args.limit, args.dry_run)


if __name__ == "__main__":
    main()
