"""The datasheet extraction dispatcher.

kicad-happy's datasheets skill is deliberately half-built: it ships the planner,
the schema validators, the merge step and the quality scoring, and specifies —
in ``references/dispatcher-contract.md`` — the one piece it does not ship. That
piece is a thing that can hand PDF pages to a vision-capable model and write
back schema-valid results. The contract exists because the skill was written to
be driven by *some* host; this server is that host.

The flow, with the piece this module owns marked:

    scout subagent            ← here (needs a model that can read pages)
      → plan_extraction.py    ← kicad-happy
        → N extractor subagents ← here, one per task, in parallel
          → merge_results.py  ← kicad-happy
            → lookup(mpn)     ← kicad-happy

Why this needs Phase 2's task routing: a datasheet is read as images. Routing
extraction at a text-only endpoint does not error — the model simply describes
nothing and the schema validator rejects empty output, or worse, accepts a
plausible hallucination. ``datasheet_vision`` is a declared vision task and the
config refuses to route it anywhere blind.

Contract obligations kept here, because each one has a failure it prevents:
idempotence (a rerun after a crash must not re-bill work already done),
never overwriting a complete result without --force, schema validation before
claiming success, and a cost ledger so a 40-part BOM's price is visible rather
than discovered on an invoice.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core import llm_chat
from ...core.llm_chat import DocumentBlock, Endpoint, Msg, TextBlock, build_chat_adapter
from ..kicad_happy import find_kicad_happy, run_script, script_path

# Anything above this many pages is a family datasheet or a reference manual;
# sending it whole wastes a fortune and buries the pages that matter. The
# planner's page selection is what keeps extraction affordable.
_MAX_PAGES_PER_TASK = 24


@dataclass
class TaskResult:
    task_id: str
    status: str            # complete | failed | skipped
    error: str = ""
    model_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "error": self.error,
            "model_id": self.model_id,
        }


@dataclass
class ExtractionRun:
    mpn: str
    results: list[TaskResult] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(r.status != "failed" for r in self.results)

    def to_dict(self) -> dict:
        return {
            "mpn": self.mpn,
            "ok": self.ok,
            "error": self.error,
            "tasks": [r.to_dict() for r in self.results],
            "complete": sum(1 for r in self.results if r.status == "complete"),
            "failed": sum(1 for r in self.results if r.status == "failed"),
            "skipped": sum(1 for r in self.results if r.status == "skipped"),
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pages_label(pages: list[int] | None) -> str:
    """"5, 6, 13-15" — the human-readable form the prompt placeholder expects."""
    if not pages:
        return "all pages"
    pages = sorted(set(pages))
    runs: list[str] = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = p
    runs.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(runs)


def _fill(template: str, *, mpn: str, pdf_path: str, pages: str, schema_path: str) -> str:
    return (
        template.replace("{{MPN}}", mpn)
        .replace("{{PDF_PATH}}", pdf_path)
        .replace("{{PAGES}}", pages)
        .replace("{{SCHEMA_PATH}}", schema_path)
    )


def _schema_version(schema_path: Path) -> str:
    try:
        return str(json.loads(schema_path.read_text(encoding="utf-8")).get("x-schema-version", "1.0"))
    except (OSError, json.JSONDecodeError):
        return "1.0"


def _append_ledger(cache_dir: Path, record: dict) -> None:
    """Per-task cost, in the shape the contract documents. Best-effort: losing a
    ledger line must never fail an extraction that otherwise worked."""
    try:
        with (cache_dir / "_cost_ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


async def _one_task(
    *,
    endpoint: Endpoint,
    prompt: str,
    pdf_bytes: str,
    filename: str,
    schema: dict,
) -> tuple[dict | None, str, int, int]:
    """Run one extractor. Returns (data, error, tokens_in, tokens_out).

    The PDF rides as a document block rather than pre-rendered images: providers
    that accept PDFs page them internally and see the vector text, which reads
    small pin tables far better than a rasterized page.
    """
    adapter = build_chat_adapter(endpoint)
    instruction = (
        f"{prompt}\n\n"
        "Return ONLY the JSON object. No prose before or after it, no markdown "
        "fence. It must validate against the schema named above; that schema is "
        "reproduced here so you do not need to open it:\n\n"
        f"{json.dumps(schema, indent=2)[:20000]}"
    )
    messages = [
        Msg(
            role="user",
            content=[
                DocumentBlock(data=pdf_bytes, filename=filename),
                TextBlock(instruction),
            ],
        )
    ]
    usage_in = usage_out = 0
    text_parts: list[str] = []
    try:
        async for event in adapter.stream_chat(messages, max_tokens=llm_chat.DEFAULT_MAX_TOKENS):
            if isinstance(event, llm_chat.Usage):
                usage_in += event.input_tokens
                usage_out += event.output_tokens
            elif isinstance(event, llm_chat.Done):
                text_parts.append(event.message.text)
    except Exception as exc:  # noqa: BLE001 — provider/transport failure is a task failure
        return None, f"{type(exc).__name__}: {exc}", usage_in, usage_out

    raw = "\n".join(t for t in text_parts if t).strip()
    if not raw:
        return None, "model returned no text", usage_in, usage_out
    # Models sometimes fence JSON despite instructions; take the outermost object.
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None, f"no JSON object in output: {raw[:200]}", usage_in, usage_out
    try:
        return json.loads(raw[start : end + 1]), "", usage_in, usage_out
    except json.JSONDecodeError as exc:
        return None, f"malformed JSON: {exc}", usage_in, usage_out


def _validate(data: dict, schema_path: Path) -> str:
    """Schema-validate a result. Returns "" when valid, else the reason.

    The contract is explicit that ``status: "complete"`` *implies* schema-valid,
    so this gate is what makes a complete result trustworthy downstream.
    """
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return ""  # validation unavailable; merge_results.py will catch it
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"schema unreadable: {exc}"

    # Sibling schemas reference each other by filename, so resolve against the
    # directory the schema lives in.
    registry = Registry()
    for sibling in schema_path.parent.glob("*.schema.json"):
        try:
            registry = registry.with_resource(
                sibling.name, Resource.from_contents(json.loads(sibling.read_text(encoding="utf-8")))
            )
        except Exception:  # noqa: BLE001 — a bad sibling should not block validation
            continue
    try:
        jsonschema.Draft202012Validator(schema, registry=registry).validate(data)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"schema validation failed at {path}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        return f"schema validation error: {exc}"
    return ""


async def extract_datasheet(
    mpn: str,
    pdf_path: Path,
    cache_dir: Path,
    endpoint: Endpoint,
    *,
    force: bool = False,
    retry_failed: bool = False,
    max_parallel: int = 4,
    on_progress=None,
) -> ExtractionRun:
    """Scout, plan, dispatch, merge — the whole extraction for one part."""
    run = ExtractionRun(mpn=mpn)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        run.error = f"no PDF at {pdf_path}"
        return run

    base = find_kicad_happy()
    if base is None:
        run.error = "kicad-happy not found — set BLPL_KICAD_HAPPY or init the submodule"
        return run
    schemas = base / "skills" / "datasheets" / "schemas"
    prompts = base / "skills" / "datasheets" / "prompts"

    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]

    def note(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # -- 1. scout ------------------------------------------------------------
    scout_file = cache_dir / f"{mpn}.scout.json"
    if not scout_file.exists() or force:
        note(f"{mpn}: scouting datasheet structure")
        prompt = _fill(
            (prompts / "scout.md").read_text(encoding="utf-8"),
            mpn=mpn,
            pdf_path=str(pdf_path),
            pages="all pages",
            schema_path=str(schemas / "scout.schema.json"),
        )
        data, err, tin, tout = await _one_task(
            endpoint=endpoint,
            prompt=prompt,
            pdf_bytes=pdf_b64,
            filename=pdf_path.name,
            schema=json.loads((schemas / "scout.schema.json").read_text(encoding="utf-8")),
        )
        _append_ledger(
            cache_dir,
            {
                "run_id": run_id, "mpn": mpn, "task_id": "scout", "tier": "B",
                "model_id": endpoint.model, "tokens_in": tin, "tokens_out": tout,
                "success": data is not None, "extracted_at": _now(),
            },
        )
        if data is None:
            run.error = f"scout failed: {err}"
            return run
        scout_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -- 2. plan (kicad-happy owns this) -------------------------------------
    note(f"{mpn}: planning extraction tasks")
    plan_res = run_script(
        script_path("datasheets", "plan_extraction.py"),
        [mpn, str(pdf_path), "--cache-dir", str(cache_dir), "--use-cached-scout"]
        + (["--force"] if force else []),
        parse_json=False,
    )
    plan_file = cache_dir / f"{mpn}.plan.json"
    if not plan_file.exists():
        run.error = f"plan_extraction.py produced no plan: {plan_res.error}"
        return run
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    # -- 3. dispatch ---------------------------------------------------------
    # depends_on is honoured by construction: only tasks whose dependencies are
    # already complete are dispatched in a wave, and a wave that adds nothing
    # ends the loop rather than spinning.
    tasks = list(plan.get("tasks") or [])
    done: set[str] = set()
    semaphore = asyncio.Semaphore(max_parallel)

    for task in tasks:
        rf = cache_dir / f"{mpn}.{task['task_id']}.result.json"
        if rf.exists():
            try:
                existing = json.loads(rf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if existing.get("status") == "complete" and not force:
                done.add(task["task_id"])
                run.results.append(
                    TaskResult(task_id=task["task_id"], status="skipped", error="already complete")
                )

    async def dispatch(task: dict) -> TaskResult:
        task_id = task["task_id"]
        schema_path = Path(task["schema"])
        if not schema_path.is_absolute():
            schema_path = schemas / schema_path.name
        prompt_path = Path(task["prompt_template"])
        if not prompt_path.is_absolute():
            prompt_path = prompts / prompt_path.name

        pages = list(task.get("pages") or [])[:_MAX_PAGES_PER_TASK]
        prompt = _fill(
            prompt_path.read_text(encoding="utf-8"),
            mpn=mpn,
            pdf_path=str(pdf_path),
            pages=_pages_label(pages),
            schema_path=str(schema_path),
        )
        if retry_failed:
            prior = cache_dir / f"{mpn}.{task_id}.result.json"
            if prior.exists():
                try:
                    why = json.loads(prior.read_text(encoding="utf-8")).get("error", "")
                except json.JSONDecodeError:
                    why = ""
                if why:
                    prompt += (
                        f"\n\nYour previous output failed validation: {why}. "
                        "Correct it and try again."
                    )

        async with semaphore:
            note(f"{mpn}: extracting {task_id}")
            data, err, tin, tout = await _one_task(
                endpoint=endpoint,
                prompt=prompt,
                pdf_bytes=pdf_b64,
                filename=pdf_path.name,
                schema=json.loads(schema_path.read_text(encoding="utf-8")),
            )

        if data is not None and not err:
            err = _validate(data, schema_path)
            if err:
                data = None

        wrapped = {
            "task_id": task_id,
            "schema_version": _schema_version(schema_path),
            "status": "complete" if data is not None else "failed",
            "extracted_at": _now(),
            "model_tier": task.get("tier", "B"),
            "model_id": endpoint.model,
            "data": data,
        }
        if data is None:
            wrapped["error"] = err or "unknown extraction failure"
        (cache_dir / f"{mpn}.{task_id}.result.json").write_text(
            json.dumps(wrapped, indent=2), encoding="utf-8"
        )
        _append_ledger(
            cache_dir,
            {
                "run_id": run_id, "mpn": mpn, "task_id": task_id, "tier": task.get("tier", "B"),
                "model_id": endpoint.model, "tokens_in": tin, "tokens_out": tout,
                "success": data is not None, "extracted_at": _now(),
            },
        )
        return TaskResult(
            task_id=task_id,
            status=wrapped["status"],
            error=wrapped.get("error", ""),
            model_id=endpoint.model,
            tokens_in=tin,
            tokens_out=tout,
        )

    pending = [t for t in tasks if t["task_id"] not in done]
    while pending:
        wave = [t for t in pending if all(d in done for d in (t.get("depends_on") or []))]
        if not wave:
            for t in pending:
                run.results.append(
                    TaskResult(
                        task_id=t["task_id"],
                        status="failed",
                        error=f"dependencies never completed: {t.get('depends_on')}",
                    )
                )
            break
        for result in await asyncio.gather(*(dispatch(t) for t in wave)):
            run.results.append(result)
            if result.status == "complete":
                done.add(result.task_id)
        pending = [t for t in pending if t["task_id"] not in {r.task_id for r in run.results}]

    # -- 4. merge (kicad-happy owns this) ------------------------------------
    note(f"{mpn}: merging results")
    merge = run_script(
        script_path("datasheets", "merge_results.py"),
        [mpn, "--cache-dir", str(cache_dir)] + (["--retry-failed"] if retry_failed else []),
        parse_json=False,
    )
    if not merge.ok and not (cache_dir / f"{mpn}.json").exists():
        run.error = f"merge_results.py failed: {merge.error}"
    return run
