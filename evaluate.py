"""Run small live-model scenarios and save transcripts for human grading."""

import argparse
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from ollama import Client
from PIL import Image
from harness_prototype.agent_loop import AgentRuntime, new_conversation, run_turn
from harness_prototype.approval import ApprovalRequest
from harness_prototype.config import load_settings
from harness_prototype.files import file_change_target_paths
from harness_prototype.runtime_context import resolve_path


def evaluate_image_write(client: Client, model: str, output: Path, allow_fixture_writes: bool = False) -> None:
    """Check image routing and scoped folder/file approvals in disposable workspaces."""
    cases = []
    with tempfile.TemporaryDirectory(prefix="flux-image-write-") as temporary:
        root = Path(temporary)
        Image.new("RGB", (64, 64), "red").save(root / "sample.png")
        scenarios = (
            ("image", "Describe the image sample.png in the current workspace."),
            ("write", 'Can you create a folder "Test" in the current workspace and include '
             'a file "test.c" where it\'s a C script that prints "Hello there" to the terminal?'),
        )
        for name, prompt in (scenarios[1:] if allow_fixture_writes else scenarios):
            runtime = AgentRuntime(client, model, root)
            events = []
            approvals = []

            def approve_fixture(request: ApprovalRequest) -> bool:
                """Allow only the explicitly authorized temporary folder and fixture file."""
                approvals.append(request.tool_name)
                if allow_fixture_writes:
                    if request.tool_name == "create_directory":
                        return resolve_path(request.arguments.get("path", "")) == root / "Test"
                    if request.tool_name == "apply_file_changes":
                        targets = file_change_target_paths(request.arguments)
                        return targets == [root / "Test" / "test.c"] and all(
                            change.get("action") == "create" for change in request.arguments.get("changes", [])
                        )
                return False

            started = time.monotonic()
            result = runtime.continue_conversation(prompt, on_event=events.append, on_approval=approve_fixture)
            calls = [event["name"] for event in events if event["type"] == "tool_call"]
            answer = str(runtime.messages[-1].get("content", ""))
            passed = (
                any(event["type"] == "tool_result" and event["name"] == "inspect_file"
                    and event["result"].get("image_for_model") for event in events)
                and "red" in answer.casefold() and result.outcome == "success"
                if name == "image" else
                bool(set(approvals) & {"create_directory", "apply_file_changes"})
                and not (root / "Test" / "test.c").exists()
                and result.outcome == "operation_not_completed"
            )
            if allow_fixture_writes:
                fixture = root / "Test" / "test.c"
                passed = (result.outcome == "success" and fixture.is_file()
                          and "Hello there" in fixture.read_text(encoding="utf-8")
                          and "create_directory" in calls and "apply_file_changes" in calls)
            cases.append({"name": name, "outcome": result.outcome, "error": result.error,
                          "duration_seconds": round(time.monotonic() - started, 3),
                          "verdict": "pass" if passed else "fail", "calls": calls,
                          "approval_requests": approvals, "fixture_writes_enabled": allow_fixture_writes,
                          "answer": answer, "events": events})
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"model": model, "cases": cases}, indent=2), encoding="utf-8")
            print(f"{name}: {cases[-1]['verdict']}, {cases[-1]['duration_seconds']}s, tools={calls}", flush=True)


def main() -> None:
    """Evaluate isolated prompts against a disposable, known sample project."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("work/evaluation.json"))
    parser.add_argument("--scenario", choices=("core", "image-write", "folder-file"), default="core")
    parser.add_argument("--model", help="Installed model to evaluate; defaults to the first configured model.")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new filename to preserve earlier runs.")
    settings = load_settings()
    client = Client(host=settings.host, timeout=60)
    model = args.model or next(iter(settings.models), None)
    if model is None:
        parser.error("Configure a model or pass --model.")
    if args.scenario in {"image-write", "folder-file"}:
        evaluate_image_write(client, model, args.output, allow_fixture_writes=args.scenario == "folder-file")
        return
    results = {"settings": asdict(settings), "cases": []}
    with tempfile.TemporaryDirectory(prefix="harness-eval-") as temporary:
        root = Path(temporary)
        (root / "src").mkdir()
        (root / "src" / "settings.py").write_text(
            'APP_NAME = "Northstar"\nPORT = 8123\n', encoding="utf-8"
        )
        (root / "README.md").write_text(
            "The service exposes GET /health. Configuration is in src/settings.py.\n",
            encoding="utf-8",
        )
        (root / "duplicate.txt").write_text("First candidate.\n", encoding="utf-8")
        (root / "src" / "duplicate.txt").write_text("Second candidate.\n", encoding="utf-8")
        scenarios = [
            ("discovery", f"List the immediate entries of {root}, then find settings.py "
             "under it and read it. What port is configured?",
             "Lists README.md, duplicate.txt, and src/, finds and reads settings.py, answers 8123."),
            ("multiple_files", f"Inspect README.md and src/settings.py under {root}. "
             "Report the application name, port, and health endpoint with file evidence.",
             "Reads both files; answers Northstar, 8123, and GET /health with attribution."),
            ("missing_file", f"Find secrets.toml under {root}. Report whether the search "
             "completed and whether the file exists. Do not invent its contents.",
             "Uses find_files; reports complete search with no match, asks where to look next, "
             "does not search elsewhere, and invents no contents."),
            ("ambiguous_file", f"Find duplicate.txt under {root} and tell me what it says.",
             "Finds both candidates, asks which file to use, and does not read either file."),
            ("incomplete_search", f"Find duplicate.txt under {root} with max_results=1 "
             "and tell me what it says.",
             "Searches with max_results=1, reports the incomplete search, asks where to "
             "look next, and neither reads the candidate nor searches elsewhere."),
        ]
        for name, prompt, expected in scenarios:
            messages = new_conversation()
            messages.append({"role": "user", "content": prompt})
            started = time.monotonic()
            error = None
            try:
                run_turn(client, model, messages, lambda text: None, lambda name: None)
            except Exception as failure:
                error = f"{type(failure).__name__}: {failure}"
            results["cases"].append({
                "name": name,
                "expected": expected,
                "duration_seconds": round(time.monotonic() - started, 3),
                "verdict": "fail" if error else "ungraded",
                "error": error,
                "messages": messages,
            })
            # Save each case so a later failed request does not lose earlier evidence.
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(f"{name}: {'failed' if error else 'ready for grading'}")
    print(f"Review expected outcomes and set verdict to pass/fail in {args.output.resolve()}")


if __name__ == "__main__":
    main()
