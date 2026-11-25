import argparse
import json
from pathlib import Path
import sys

from .builder import build_file
from .container import HmiProject, atomic_write
from .project import inspect_project, validate_project


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect and build template-based TJC HMI projects")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Print resource and page metadata as JSON")
    inspect.add_argument("project")
    inspect.add_argument("--output", help="Save JSON to this path")
    export = commands.add_parser("export", help="Extract resource payloads")
    export.add_argument("project")
    export.add_argument("--output", required=True)
    validate = commands.add_parser("validate", help="Validate page CRCs and resource structure")
    validate.add_argument("project")
    validate.add_argument("--greenhouse", action="store_true")
    validate.add_argument("--pictures", action="store_true")
    build = commands.add_parser("build", help="Generate the four-page greenhouse project")
    build.add_argument("--template", required=True)
    build.add_argument("--controls-template", required=True)
    build.add_argument("--layout", required=True)
    build.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            project = build_file(args.template, args.controls_template, args.layout, args.output)
            result = {"output": args.output, "bytes": len(project.raw),
                      "validation": validate_project(project, True, True)}
        else:
            project = HmiProject.load(args.project)
            if args.command == "inspect":
                result = inspect_project(project)
            elif args.command == "export":
                project.export(args.output)
                result = {"exported": len(project.resources), "output": args.output}
            else:
                result = validate_project(project, args.greenhouse, args.pictures)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.command == "inspect" and args.output:
            if Path(args.output).resolve() == Path(args.project).resolve():
                raise ValueError("Inspection output must differ from the input project")
            atomic_write(args.output, text.encode("utf-8"))
        else:
            print(text)
        return 0
    except (OSError, ValueError, KeyError, OverflowError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
