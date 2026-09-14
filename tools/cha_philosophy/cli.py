"""CLI entry point. All source-service operations are read-only."""
import argparse
import json
import os
import sys
from pathlib import Path

from .store import DEFAULT_HOME, DOMAINS, Store


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def save_or_emit(value, path=None):
    if path is None:
        emit(value)
        return
    # Output paths are explicit; never overwrite a manuscript or earlier result.
    path=Path(path).expanduser().resolve()
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"w") as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2)
        handle.write("\n")
    emit({"output_path":str(path),"status":value.get("status","prepared")})


def task_inputs(args):
    # Universal-newline text reads silently change original offsets and hashes.
    request=args.request.read_bytes().decode("utf-8")
    inputs=[]
    for spec in args.input:
        kind,sep,path=spec.partition("=")
        if not sep: raise ValueError("input must be KIND=PATH")
        inputs.append({"kind":kind,"content":Path(path).expanduser().read_bytes().decode("utf-8")})
    return request,inputs


def read_task_json(path):
    """Reject ambiguous JSON before provenance or coverage checks see it."""
    from .task import TaskError

    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise TaskError("ambiguous_task_json")
            result[key]=value
        return result

    def invalid_constant(value):
        raise TaskError("invalid_task_json_number")

    return json.loads(Path(path).read_bytes().decode("utf-8"),
                      object_pairs_hook=unique,parse_constant=invalid_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cha-philosophy")
    parser.add_argument("--home",type=Path,default=DEFAULT_HOME)
    sub = parser.add_subparsers(dest="command",required=True)
    sub.add_parser("status")
    p=sub.add_parser("teams-auth");p.add_argument("action",choices=["status","login"])
    p.add_argument("--config",type=Path)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--limit",type=int,default=10)
    p.add_argument("--include-context",action="store_true")
    p = sub.add_parser("source"); p.add_argument("source_id")
    p = sub.add_parser("principles"); p.add_argument("--status")
    p = sub.add_parser("bundle"); p.add_argument("task",choices=sorted(DOMAINS)); p.add_argument("query")
    p.add_argument("--limit",type=int,default=8)
    p = sub.add_parser("import"); p.add_argument("file",type=Path)
    p = sub.add_parser("propose"); p.add_argument("file",type=Path)
    p = sub.add_parser("review"); p.add_argument("principle_id"); p.add_argument("decision")
    p.add_argument("--reviewer",required=True); p.add_argument("--reason",required=True)
    p.add_argument("--professor-attestation",action="store_true")
    p = sub.add_parser("support"); p.add_argument("principle_id"); p.add_argument("file",type=Path)
    p.add_argument("--reviewer",required=True); p.add_argument("--reason",required=True)
    p = sub.add_parser("conflict"); p.add_argument("left"); p.add_argument("right")
    p = sub.add_parser("resolve"); p.add_argument("keep"); p.add_argument("supersede")
    p.add_argument("--reviewer",required=True);p.add_argument("--reason",required=True)
    p = sub.add_parser("revoke"); p.add_argument("source_id")
    sub.add_parser("source-exclusions")
    for name in ("exclude-source", "clear-source-exclusion"):
        p=sub.add_parser(name);p.add_argument("source_id")
        p.add_argument("--source-hash",required=True)
        p.add_argument("--reviewer",required=True);p.add_argument("--reason",required=True)
    for name in ("hold-for-evaluation","release-evaluation"):
        p=sub.add_parser(name);p.add_argument("source_id");p.add_argument("--reason",required=True)
    p = sub.add_parser("sync"); p.add_argument("--config",type=Path)
    p.add_argument("--platform",choices=["all","gmail","drive","teams","teams_archive"],default="all")
    p.add_argument("--limit",type=int,default=100)
    p = sub.add_parser("distill"); p.add_argument("--limit",type=int,default=10)
    p.add_argument("--model",default="qwen3.6:35b-ctx16k")
    p = sub.add_parser("audit"); p.add_argument("output",type=Path); p.add_argument("bundle",type=Path)
    p = sub.add_parser("audit-task"); p.add_argument("output",type=Path); p.add_argument("bundle",type=Path)
    p.add_argument("--coverage",type=Path)
    p=sub.add_parser("plan-task");p.add_argument("bundle",type=Path)
    p.add_argument("--unit-bytes",type=int,default=6000);p.add_argument("--output",type=Path)
    p=sub.add_parser("read-task-unit");p.add_argument("bundle",type=Path);p.add_argument("unit_id")
    p.add_argument("--output",type=Path)
    for command in ("prepare","apply"):
        p=sub.add_parser(command);p.add_argument("task",choices=sorted(DOMAINS))
        p.add_argument("--request",type=Path,required=True)
        p.add_argument("--input",action="append",default=[],metavar="KIND=PATH")
        p.add_argument("--output",type=Path)
        if command=="prepare":p.add_argument("--target",choices=["app","local"],default="app")
        if command=="apply":p.add_argument("--model",default="qwen3.6:35b-ctx16k")
    p=sub.add_parser("refresh");p.add_argument("--config",type=Path)
    p.add_argument("--gmail-limit",type=int,default=100);p.add_argument("--drive-limit",type=int,default=10)
    p.add_argument("--distill-limit",type=int,default=3);p.add_argument("--model",default="qwen3.6:35b-ctx16k")
    args = parser.parse_args(argv)
    store = Store(args.home)
    try:
        if args.command == "status": emit(store.status())
        elif args.command == "teams-auth":
            from .teams_auth import TeamsAuth,auth_status
            from .task import TaskError
            configuration=read_task_json(args.config or store.home/'config.json')
            if args.action=='status':
                emit(auth_status(configuration,store.home))
            else:
                # Device codes belong only in a terminal the user explicitly
                # opened. Never send them to a timer, redirected log or journal.
                if not all(stream.isatty() for stream in (sys.stdin,sys.stdout,sys.stderr)):
                    raise TaskError('teams_login_requires_interactive_terminal')
                def display(prompt):
                    print('Open '+prompt.verification_uri+' and enter code '+prompt.user_code,
                          file=sys.stderr,flush=True)
                emit(TeamsAuth(configuration,store.home).login_device_code(display))
        elif args.command == "source": emit(store.get_source(args.source_id))
        elif args.command == "search": emit(store.search(args.query,args.limit,direct=not args.include_context))
        elif args.command == "principles": emit(store.principles(args.status))
        elif args.command == "bundle":
            from .evaluation import build_task_instructions
            result = store.bundle(args.task,args.query,args.limit)
            result["task_instructions"] = build_task_instructions(args.task)
            emit(result)
        elif args.command == "import":
            n=changed=0
            for line in args.file.read_text().splitlines():
                if line.strip(): changed += store.upsert(json.loads(line)); n+=1
            emit({"read":n,"changed":changed})
        elif args.command == "propose":
            value=json.loads(args.file.read_text()); rows=value if isinstance(value,list) else [value]
            emit({"principle_ids":[store.add_principle(p) for p in rows]})
        elif args.command == "review":
            store.review(args.principle_id,args.decision,args.reviewer,args.reason,
                         professor_attestation=args.professor_attestation)
            emit({"principle_id":args.principle_id,"decision":args.decision})
        elif args.command == "support":
            emit(store.add_support(args.principle_id,read_task_json(args.file),args.reviewer,args.reason))
        elif args.command == "conflict":
            store.conflict(args.left,args.right); emit({"status":"disputed"})
        elif args.command == "resolve":
            store.resolve(args.keep,args.supersede,args.reviewer,args.reason);emit({"status":"resolved_pending_review"})
        elif args.command == "revoke":
            store.revoke(args.source_id); emit({"status":"unavailable"})
        elif args.command == "source-exclusions":
            emit(store.source_exclusions())
        elif args.command in {"exclude-source", "clear-source-exclusion"}:
            emit(store.review_source_exclusion(args.source_id, source_hash=args.source_hash,
                                              exclude=args.command=="exclude-source",
                                              reviewer=args.reviewer, reason=args.reason))
        elif args.command=="hold-for-evaluation":
            store.hold_for_evaluation(args.source_id,args.reason);emit({"status":"evaluation_family_reserved"})
        elif args.command=="release-evaluation":
            store.release_evaluation(args.source_id,args.reason);emit({"status":"evaluation_family_released"})
        elif args.command == "sync":
            from .sync import sync,has_operation_errors
            config=json.loads((args.config or args.home/"config.json").read_text())
            result=sync(store,config,args.platform,args.limit)
            emit(result)
            return 1 if any(has_operation_errors(r) for r in result.get("results",[])) else 0
        elif args.command == "distill":
            from .runner import distill_pending
            emit(distill_pending(store,limit=args.limit,model=args.model))
        elif args.command=="refresh":
            from .refresh import refresh
            from .sync import has_operation_errors
            config=json.loads((args.config or args.home/"config.json").read_text())
            result=refresh(store,config,gmail_limit=args.gmail_limit,drive_limit=args.drive_limit,
                           distill_limit=args.distill_limit,model=args.model)
            emit(result)
            failed=(result.get("state")=="error" or
                    any(has_operation_errors(r) for r in result.get("sources",{}).values()) or
                    has_operation_errors(result.get("inference",{})))
            return 1 if failed else 0
        elif args.command in {"prepare","apply"}:
            from .task import prepare_task,generate_task
            request,inputs=task_inputs(args)
            if args.command=="prepare":
                from .evaluation import build_task_instructions
                result=prepare_task(store,args.task,request,inputs,target=args.target)
                result["task_instructions"]=build_task_instructions(args.task)
            else:
                from .distill import LocalOllamaClient
                result=generate_task(store,args.task,request,inputs,client=LocalOllamaClient(args.model))
            save_or_emit(result,args.output)
            return 1 if result.get("status")=="invalid" else 0
        elif args.command=="audit-task":
            from .task import audit_task_output
            coverage_args={"coverage":read_task_json(args.coverage)} if args.coverage else {}
            result=audit_task_output(store,read_task_json(args.bundle),read_task_json(args.output),
                                     **coverage_args)
            emit(result)
            return 1 if result["status"]=="invalid" else 0
        elif args.command in {"plan-task","read-task-unit"}:
            from .task import TaskError
            from .task_receipts import validate_task_receipt
            from .task_coverage import plan_task_coverage,read_task_unit,coverage_schema
            bundle=read_task_json(args.bundle)
            errors=validate_task_receipt(store,bundle)
            if errors:raise TaskError(errors[0])
            result=(plan_task_coverage(bundle,max_unit_bytes=args.unit_bytes) if args.command=="plan-task"
                    else read_task_unit(store,bundle,args.unit_id))
            if args.command=="plan-task":result["coverage_schema"]=coverage_schema(result)
            save_or_emit(result,args.output)
        elif args.command == "audit":
            from .evaluation import audit_output
            bundle=json.loads(args.bundle.read_text())
            errors=store.validate_bundle(bundle)
            result=audit_output(json.loads(args.output.read_text()),bundle)
            if errors:
                result["status"]="invalid";result.setdefault("errors",[]).extend(errors)
            emit(result)
            return 1 if result["status"]=="invalid" else 0
        return 0
    except Exception as exc:
        # No connector stderr or source text in error logs.
        from .task import TaskError
        from .task_coverage import TaskCoverageError
        from .teams_auth import TeamsAuthError
        error={"status":"error","error_type":type(exc).__name__}
        if isinstance(exc,TaskError):error.update(error_code=exc.code,needs_chunking=exc.needs_chunking)
        elif isinstance(exc,TaskCoverageError):error.update(error_code=exc.code)
        elif isinstance(exc,TeamsAuthError):error.update(error_code=exc.code)
        emit(error)
        return 1
    finally:
        store.close()
