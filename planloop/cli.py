"""Command line entry points.

    planloop demo                 # the four-run memory demonstration
    planloop validate spec.json   # intake + compile, no execution
    planloop memory list          # inspect what has been learned
    planloop run spec.json -w dir # run against a real workspace (needs a model)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from planloop.compile import compile_plan
from planloop.decomp import build_plan
from planloop.memory import MemoryStore, signature
from planloop.model import make_model_from_env
from planloop.scheduler import run
from planloop.spec import ProblemSpec, SpecError


def _cmd_demo(args: argparse.Namespace) -> int:
    from planloop.demo import run_demo

    run_demo(Path(args.dir), quiet=args.quiet)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    spec = ProblemSpec.from_dict(json.loads(Path(args.spec).read_text(encoding="utf-8")))
    try:
        spec.require_ready()
    except SpecError as exc:
        print(f"not ready: {exc}")
        return 2
    model = make_model_from_env()
    plan = build_plan(spec, model=model)
    report = compile_plan(spec, plan)
    print(report.render())
    return 0 if report.ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    model = make_model_from_env()
    if model is None:
        print("no model configured: set PLANLOOP_MODEL (and PLANLOOP_BASE_URL / PLANLOOP_API_KEY)", file=sys.stderr)
        return 2
    from planloop.persist import find_state, new_run_id, runs_root

    workspace = Path(args.workspace)
    memory = MemoryStore(args.memory) if args.memory else None

    from planloop.sandbox import SandboxError
    from planloop.sandbox import build as build_sandbox

    try:
        sandbox = build_sandbox(args.sandbox)
    except SandboxError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    git_ws = None
    if args.git:
        from planloop.workspace import GitError, GitWorkspace

        if not GitWorkspace.available():
            print("git is not available on PATH", file=sys.stderr)
            return 2
        git_ws = GitWorkspace(workspace)
        try:
            git_ws.start  # noqa: B018 - attribute check keeps the import honest
        except GitError:  # pragma: no cover - defensive
            return 2

    if args.resume:
        state_path = find_state(workspace, None if args.resume in ("", "latest") else args.resume)
        result = run(
            None,
            model=model,
            workspace=workspace,
            memory=memory,
            run_dir=state_path.parent,
            resume=True,
            git=git_ws,
            auto_approve=args.auto_approve,
            sandbox=sandbox,
        )
    else:
        if not args.spec:
            print("need a spec file (or --resume)", file=sys.stderr)
            return 2
        spec = ProblemSpec.from_dict(json.loads(Path(args.spec).read_text(encoding="utf-8")))
        run_dir = Path(args.runs_dir) / new_run_id(spec.goal) if args.runs_dir else \
            runs_root(workspace) / new_run_id(spec.goal)
        result = run(
            spec,
            model=model,
            workspace=workspace,
            memory=memory,
            run_dir=run_dir,
            git=git_ws,
            allow_dirty=args.allow_dirty,
            auto_approve=args.auto_approve,
            sandbox=sandbox,
        )

    print(result.render())
    if args.json:
        Path(args.json).write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if result.ok else 1


def _cmd_runs(args: argparse.Namespace) -> int:
    from planloop.persist import list_runs, runs_root

    rows = list_runs(Path(args.workspace))
    if not rows:
        print(f"(no runs under {runs_root(Path(args.workspace))})")
        return 0
    print(f"{'run_id':<40}{'status':<12}{'nodes':<8}{'goal'}")
    for row in rows:
        print(
            f"{row['run_id']:<40}{row['status']:<12}"
            f"{row['nodes_done']}/{row['nodes_total']:<6}{row['goal']}"
        )
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    store = MemoryStore(args.memory)
    if args.action == "list":
        print(json.dumps(store.summary(), indent=2, ensure_ascii=False))
        for lesson in store.active():
            print(
                f"- {lesson.id} [{lesson.strength} trust={lesson.trust:.2f}] {lesson.summary}\n"
                f"    trigger={lesson.trigger.to_dict()}\n"
                f"    guard={lesson.guard}"
            )
        return 0
    if not args.id:
        print("need a lesson id", file=sys.stderr)
        return 2
    lesson = store.lessons.get(args.id)
    if lesson is None:
        print(f"no such lesson: {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(lesson.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _cmd_signature(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    sig, norm = signature(text)
    print(json.dumps({"signature": sig, "normalized": norm}, ensure_ascii=False, indent=2))
    return 0


def _cmd_mem(args: argparse.Namespace) -> int:
    from planloop.memdb import MemorySystem

    system = MemorySystem(args.root)
    try:
        if args.action == "add":
            evidence = None
            if args.evidence_command:
                evidence = {"type": "command", "spec": args.evidence_command}
            elif args.evidence_file:
                evidence = {"type": "file", "path": args.evidence_file}
            mid = system.remember(
                args.kind,
                args.key,
                args.value,
                public=args.public,
                evidence=evidence,
                confidence=args.confidence,
                source="human",
                exec_spec=args.exec_spec or None,
            )
            print(f"stored {mid} in {'public' if args.public else 'private'}")
            return 0
        if args.action == "search":
            hits = system.search(args.query, limit=args.limit)
            for hit in hits:
                print(hit.render())
            if not hits:
                print("(no matches)")
            return 0
        if args.action == "list":
            for db in (system.public, system.private):
                for row in db.rows():
                    flag = "ok" if row["evidence"] else "??"
                    print(f"[{db.visibility}:{flag}] {row['kind']:<10} {row['key']:<24} {row['value'][:70]}")
            return 0
        if args.action == "stats":
            print(json.dumps(system.stats(), indent=2, ensure_ascii=False))
            return 0
        if args.action == "verify":
            outcome = system.verify()
            print(json.dumps(outcome, indent=2, ensure_ascii=False))
            bad = [r for results in outcome.values() for r in results if not r["ok"]]
            return 1 if bad else 0
        if args.action == "promote":
            if not args.id:
                print("need an id", file=sys.stderr)
                return 2
            row = system.private.conn.execute("SELECT * FROM memory WHERE id = ?", (args.id,)).fetchone()
            if row is None:
                print(f"no such memory: {args.id}", file=sys.stderr)
                return 1
            if not args.yes:
                print(
                    f"would promote {row['key']} -> public (shared with the team). Pass --yes to confirm.",
                    file=sys.stderr,
                )
                return 2
            system.public.upsert(
                row["kind"], row["key"], row["value"],
                evidence=json.loads(row["evidence"]) if row["evidence"] else None,
                confidence=row["confidence"], source=row["source"],
            )
            system.private.set_status(args.id, "archived")
            print(f"promoted {args.id} to public")
            return 0
        if args.action == "forget":
            if not args.id:
                print("need an id", file=sys.stderr)
                return 2
            for db in (system.public, system.private):
                db.forget(args.id, reason="user forget")
            print(f"archived {args.id}")
            return 0
        if args.action == "history":
            mid = args.id or args.query or ""
            if not mid:
                print("need an id", file=sys.stderr)
                return 2
            found = False
            for db in (system.public, system.private):
                for entry in db.history(mid):
                    found = True
                    print(
                        f"{entry['op']:<7} {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['at']))} "
                        f"conf={entry['confidence']:.2f} {entry['reason'] or ''}"
                    )
                    if entry["op"] == "UPDATE":
                        print(f"        旧: {entry['old_value']}")
                        print(f"        新: {entry['new_value']}")
            if not found:
                print(f"no history for {mid}", file=sys.stderr)
                return 1
            return 0
        if args.action == "export":
            tiers = [t.strip().upper() for t in (args.tiers or "A,B").split(",") if t.strip()]
            risky = sorted(set(tiers) - {"A", "B"})
            if risky and not args.force:
                print(
                    f"refusing: tier(s) {','.join(risky)} are not machine-verified. "
                    "Exported memory becomes instructions for the next agent; pass --force to include them.",
                    file=sys.stderr,
                )
                return 2
            text = system.export_markdown(include_private=args.private, tiers=tiers)
            if args.out:
                target = Path(args.out)
                if target.exists() and not args.yes:
                    print(f"{target} exists; pass --yes to overwrite", file=sys.stderr)
                    return 2
                target.write_text(text, encoding="utf-8")
                print(f"wrote {target} ({len(text)} chars, tiers {','.join(tiers)})")
            else:
                print(text)
            return 0
        if args.action == "run":
            if not args.query:
                print("need a procedure key", file=sys.stderr)
                return 2
            ok, output = system.private.run_procedure(args.query)
            print(output)
            return 0 if ok else 1
    finally:
        system.close()
    return 2


def _cmd_audit(args: argparse.Namespace) -> int:
    from planloop.audit import build_report
    from planloop.persist import find_state
    from planloop.workspace import GitWorkspace

    state_path = find_state(Path(args.workspace), args.run_id or None)
    git = GitWorkspace(Path(args.workspace)) if args.git else None
    report = build_report(state_path.parent, git=git)
    text = report.to_markdown()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def _cmd_why(args: argparse.Namespace) -> int:
    from planloop.blame import blame, find_run_dir

    target, line = args.target, None
    if ":" in target:
        head, tail = target.rsplit(":", 1)
        if tail.isdigit():
            target, line = head, int(tail)
    run_dir = find_run_dir(Path(args.workspace), args.run_id or None)
    report = blame(Path(args.workspace), target, line=line, run_dir=run_dir)
    print(report.to_markdown())
    return 0


def _cmd_approvals(args: argparse.Namespace) -> int:
    from planloop.approvals import ApprovalStore

    store = ApprovalStore(Path(args.workspace) / ".planloop" / "approvals.jsonl")
    if args.action == "list":
        rows = store.pending() if args.pending else store.all()
        if not rows:
            print("(no approvals)")
            return 0
        for approval in rows:
            print(approval.describe())
        return 0
    if not args.token:
        print("need a token", file=sys.stderr)
        return 2
    try:
        if args.action == "approve":
            approval = store.approve(args.token, by=args.by or "human")
            print(f"approved {approval.token}: {approval.target}")
            print(f"next: planloop run --resume -w {args.workspace}")
            return 0
        if args.action == "deny":
            approval = store.deny(args.token, by=args.by or "human")
            print(f"denied {approval.token}: {approval.target}")
            return 0
    except KeyError:
        print(f"no such approval: {args.token}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def _local_api_key(workspace: Path | str) -> str:
    """Read the key from the git-ignored credentials file, if present."""
    candidate = Path(workspace) / ".planloop" / "credentials.env"
    if not candidate.exists():
        return ""
    for line in candidate.read_text(encoding="ascii").splitlines():
        if line.startswith("PLANLOOP_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def _cmd_bench(args: argparse.Namespace) -> int:
    import planloop.bench as bench

    if args.real:
        model = args.model or os.environ.get("PLANLOOP_MODEL") or "MiniMax-M3"
        base = args.base_url or os.environ.get("PLANLOOP_BASE_URL") or "https://api.minimax.chat/v1"
        key = os.environ.get("PLANLOOP_API_KEY") or _local_api_key(args.workspace)
        if not key:
            print("no API key: set PLANLOOP_API_KEY or .planloop/credentials.env", file=sys.stderr)
            return 2
        bench.MODEL_FACTORY = bench.real_model_factory(model, base, key)
        print(f"using real model {model} @ {base}")

    tasks = [t for t in bench.TASKS if not args.tasks or t.key in args.tasks.split(",")]
    if not tasks:
        print(f"no tasks matched {args.tasks!r}", file=sys.stderr)
        return 2

    report = bench.benchmark(
        reps=args.reps, p_correct=args.p_correct, seed=args.seed, tasks=tasks,
        n_best=args.n_best, configs=tuple(c.strip().upper() for c in args.configs.split(",") if c.strip()),
    )
    text = report.to_markdown()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="planloop", description="acceptance-centric plan-graph runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the memory demonstration")
    demo.add_argument("--dir", default="demo-run")
    demo.add_argument("--quiet", action="store_true")
    demo.set_defaults(func=_cmd_demo)

    validate = sub.add_parser("validate", help="intake + compile a spec without executing")
    validate.add_argument("spec")
    validate.set_defaults(func=_cmd_validate)

    run_p = sub.add_parser("run", help="execute a spec against a workspace")
    run_p.add_argument("spec", nargs="?")
    run_p.add_argument("-w", "--workspace", default=".")
    run_p.add_argument("--memory", default=".planloop/memory.jsonl")
    run_p.add_argument("--json", default="")
    run_p.add_argument("--runs-dir", default="", help="where run state is stored")
    run_p.add_argument("--git", action="store_true", help="commit one checkpoint per finished node")
    run_p.add_argument("--allow-dirty", action="store_true", help="stash uncommitted work before starting")
    run_p.add_argument("--auto-approve", action="store_true", help="pass milestone gates without a human")
    run_p.add_argument("--sandbox", choices=["host", "none", "docker"], default="host",
                       help="where commands run (none = refuse to execute)")
    run_p.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default="",
        help="resume a run (default: the most recent one)",
    )
    run_p.set_defaults(func=_cmd_run)

    runs = sub.add_parser("runs", help="list persisted runs")
    runs.add_argument("-w", "--workspace", default=".")
    runs.set_defaults(func=_cmd_runs)

    audit = sub.add_parser("audit", help="markdown audit report for a run")
    audit.add_argument("run_id", nargs="?", default="")
    audit.add_argument("-w", "--workspace", default=".")
    audit.add_argument("--git", action="store_true", help="include commits and stashes")
    audit.add_argument("--out", default="")
    audit.set_defaults(func=_cmd_audit)

    why = sub.add_parser("why", help="which node changed a file (or a line)")
    why.add_argument("target", help="path, optionally path:line")
    why.add_argument("-w", "--workspace", default=".")
    why.add_argument("--run", dest="run_id", default="", help="run id (default: latest)")
    why.set_defaults(func=_cmd_why)

    bench = sub.add_parser("bench", help="B0/B1/B2 comparison on three tasks")
    bench.add_argument("--reps", type=int, default=5)
    bench.add_argument("--p-correct", type=float, default=0.6, dest="p_correct")
    bench.add_argument("--seed", type=int, default=0)
    bench.add_argument("--out", default="")
    bench.add_argument("--real", action="store_true", help="use a real model instead of the scripted one")
    bench.add_argument("--model", default="")
    bench.add_argument("--base-url", default="", dest="base_url")
    bench.add_argument("--n-best", type=int, default=3, dest="n_best")
    bench.add_argument("--tasks", default="", help="comma separated task keys")
    bench.add_argument("--configs", default="B0,B1,B2", help="which configs to run")
    bench.add_argument("-w", "--workspace", default=".")
    bench.set_defaults(func=_cmd_bench)

    approvals = sub.add_parser("approvals", help="review high-risk actions")
    approvals.add_argument("action", choices=["list", "approve", "deny"])
    approvals.add_argument("token", nargs="?")
    approvals.add_argument("-w", "--workspace", default=".")
    approvals.add_argument("--pending", action="store_true", help="list only pending")
    approvals.add_argument("--by", default="", help="who approved (recorded in the audit log)")
    approvals.set_defaults(func=_cmd_approvals)

    memory = sub.add_parser("memory", help="inspect learned lessons")
    memory.add_argument("action", choices=["list", "show"])
    memory.add_argument("id", nargs="?")
    memory.add_argument("--memory", default=".planloop/memory.jsonl")
    memory.set_defaults(func=_cmd_memory)

    sig = sub.add_parser("signature", help="show how a failure text is normalised")
    sig.add_argument("file", nargs="?")
    sig.set_defaults(func=_cmd_signature)

    mem = sub.add_parser("mem", help="persistent memory (SQLite + FTS5 + vectors)")
    mem.add_argument(
        "action",
        choices=["add", "search", "list", "stats", "verify", "promote", "forget", "history", "export", "run"],
    )
    mem.add_argument("query", nargs="?")
    mem.add_argument("--root", default=".")
    mem.add_argument("--kind", default="fact")
    mem.add_argument("--key", default="")
    mem.add_argument("--value", default="")
    mem.add_argument("--evidence-command", default="")
    mem.add_argument("--evidence-file", default="")
    mem.add_argument("--confidence", type=float, default=0.8)
    mem.add_argument("--exec", dest="exec_spec", default="", help="shell command for a procedure")
    mem.add_argument("--out", default="", help="export target (default: stdout)")
    mem.add_argument("--tiers", default="A,B", help="export: evidence tiers A,B,C,D (default A,B)")
    mem.add_argument("--yes", action="store_true", help="confirm overwrite / promotion")
    mem.add_argument("--force", action="store_true", help="allow exporting non-machine-verified tiers")
    mem.add_argument("--public", action="store_true", help="store in the shareable database")
    mem.add_argument("--private", action="store_true", help="export: include private entries")
    mem.add_argument("--limit", type=int, default=6)
    mem.add_argument("--id", default="")
    mem.set_defaults(func=_cmd_mem)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:  # keep Chinese report text readable when output is redirected
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
