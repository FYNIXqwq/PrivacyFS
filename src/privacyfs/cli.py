"""privacyfs CLI.

Commands:
    privacyfs scan D:\\path --format tree|json   anonymized listing
    privacyfs analyze D:\\path                    context inference-risk audit
    privacyfs mirror D:\\path E:\\view           anonymized mirror directory
    privacyfs map show                           reveal the local mapping
    privacyfs map reset                          drop all aliases
"""

from __future__ import annotations

import sys
import sqlite3
from functools import wraps
from pathlib import Path

import typer
from rich.console import Console

from .config import Rules, load_rules
from .core import (
    build_scan_result,
    detect_entries,
    iter_mapped,
    to_result,
    transform_entries,
    walk_entries,
)
from .emit.audit import analysis_to_json, render_analysis
from .emit.listing import mappings_to_json, render_tree, stats_line, write_json
from .emit.mirror import MirrorBuildError, build_mirror
from .risk import analyze_entries
from .policy import (
    PolicyRejected,
    blur_mapped_structure,
    blur_scan_result,
    bucket_count,
    enforce_context_policy,
    strip_context_signals,
)
from .pseudonym import Pseudonymizer, default_db_path
from .scanner import norm_path

app = typer.Typer(help="Scan directories and emit anonymized file listings.")
map_app = typer.Typer(help="Manage the real-name <-> alias mapping (kept local).")
app.add_typer(map_app, name="map")
release_app = typer.Typer(help="Inspect or recover local D3 publications")
app.add_typer(release_app, name="release")
workspace_app = typer.Typer(help="D4 local workspaces, corrections and incremental review")
app.add_typer(workspace_app, name="workspace")

console = Console()
err = Console(stderr=True)


def _safe_errors(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, sqlite3.Error):
            err.print("[red]filesystem or database operation failed; output not confirmed[/]")
            raise typer.Exit(4) from None
        except (ValueError, UnicodeError):
            err.print("[red]invalid configuration or mapping; operation refused[/]")
            raise typer.Exit(2) from None
    return guarded


def _prepare_rules(
    rules: Path | None,
    no_ner: bool,
    no_org: bool,
    no_prof: bool,
    no_en: bool,
    paranoid: bool,
    ner: str | None = None,
    no_pinyin: bool = False,
    no_identity: bool = False,
    llm: bool = False,
    llm_model: str | None = None,
    llm_url: str | None = None,
) -> Rules:
    try:
        rule_set = load_rules(str(rules) if rules else None)
    except (OSError, ValueError) as exc:
        err.print("[red]bad rules file: invalid syntax, value, or inaccessible file[/]")
        raise typer.Exit(2)
    if ner is not None:
        if ner not in ("auto", "hanlp", "jieba", "off"):
            err.print(f"[red]--ner must be auto|hanlp|jieba|off[/]")
            raise typer.Exit(2)
        rule_set.ner_engine = ner
        rule_set.use_ner = ner != "off"
    if no_ner:
        rule_set.ner_engine = "off"
        rule_set.use_ner = False
    if no_org:
        rule_set.detect_orgs = False
    if no_prof:
        rule_set.detect_professions = False
    if no_en:
        rule_set.detect_en_names = False
    if no_pinyin:
        rule_set.detect_pinyin = False
    if no_identity:
        rule_set.detect_identity = False
    if llm:
        rule_set.use_llm = True
    if llm_model:
        rule_set.llm_model = llm_model
    if llm_url:
        rule_set.llm_url = llm_url
    if paranoid:
        rule_set.paranoid = True
    return rule_set


def _exclusions_for(db_path: Path, root: Path | None = None) -> frozenset[str]:
    """The mapping DB must never appear in its own output. Only pay the
    per-entry norm_path check when the DB is actually inside the tree."""
    if root is not None:
        try:
            if not db_path.resolve().is_relative_to(root.resolve()):
                return frozenset()
        except OSError:
            return frozenset()
    return frozenset(norm_path(str(db_path.resolve()) + suffix)
                     for suffix in ("", "-journal", "-wal", "-shm"))


def _massage_argv(argv: list[str]) -> list[str]:
    """click can't express "flag with optional value" through typer, so give
    a bare `-r`/`--recursive` (not followed by an integer) the explicit value
    0 (= unlimited) before parsing. `-r2` and `--recursive=2` already work."""
    out: list[str] = []
    for i, tok in enumerate(argv):
        out.append(tok)
        if tok in ("-r", "--recursive"):
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None or not nxt.lstrip("-").isdigit():
                out.append("0")
    return out


def entrypoint() -> None:
    import sys as _sys

    argv = _sys.argv[1:]
    # bare `privacyfs -r 2` / `privacyfs D:\dir` => treat as `scan ...`;
    # real subcommands and top-level options pass through untouched
    _top = {"scan", "analyze", "mirror", "map", "tui", "inspect", "relate", "prepare", "review", "export", "release", "workspace", "doctor", "sample", "--help", "--install-completion", "--show-completion"}
    if argv and argv[0] not in _top:
        argv.insert(0, "scan")
    _sys.argv[1:] = _massage_argv(argv)
    app()


def _write_ignore_files(root: Path, mapped) -> int:
    """Drop .ignore files naming each sensitive entry, so well-behaved tools
    (rg, Claude Code's Glob) skip them. Writes into the REAL tree."""
    marks: dict[Path, set[str]] = {}
    for e in mapped:
        if e.aliases or e.hidden:
            parent = (root / e.raw).parent
            marks.setdefault(parent, set()).add(e.raw.name)
    written = 0
    for parent, names in marks.items():
        ignore = parent / ".ignore"
        existing = set()
        if ignore.exists():
            existing = set(ignore.read_text(encoding="utf-8").splitlines())
        new = sorted(names - existing)
        if new:
            with open(ignore, "a", encoding="utf-8") as f:
                f.write("\n".join(new) + "\n")
            written += 1
    return written


def _context_mapped(
    root: Path,
    rule_set: Rules,
    pseudo: Pseudonymizer,
    exclude_paths: frozenset[str],
    progress,
    *,
    with_size: bool,
    max_level: int | None,
    jobs: int,
    context_mode: str,
):
    """Build mapped entries, optionally auditing or enforcing full-tree context."""
    if context_mode not in ("off", "audit", "enforce"):
        err.print("[red]--context-mode must be off|audit|enforce[/]")
        raise typer.Exit(2)
    if context_mode == "off":
        return iter_mapped(
            root, rule_set, pseudo, exclude_paths, progress,
            with_size=with_size, max_level=max_level, jobs=jobs,
        ), None
    if not rule_set.context_enabled:
        err.print("[red]context analysis is disabled by the rules file[/]")
        raise typer.Exit(2)

    import secrets

    namespace = secrets.token_hex(32)
    # Policy decisions always inspect the complete visible tree. max_level is
    # applied only after the decision so a shallow listing cannot hide risks.
    raws, dir_sizes = walk_entries(
        root, rule_set, exclude_paths=exclude_paths, progress=progress,
        with_size=with_size, max_level=None, jobs=jobs,
    )
    detected = detect_entries(
        raws, rule_set, pseudo=pseudo, dir_sizes=dir_sizes, progress=progress,
        max_level=None, jobs=jobs, entry_namespace=namespace,
    )
    if context_mode == "audit":
        analysis = analyze_entries(detected, rule_set, namespace=namespace)
        high = sum(risk.severity == "high" for risk in analysis.risks)
        err.print(
            f"[yellow]context audit:[/] {len(analysis.risks)} risk(s), "
            f"{high} high; output is not context-transformed"
        )
        strip_context_signals(detected)
        selected = detected
        policy = analysis
    else:
        try:
            policy = enforce_context_policy(detected, rule_set, namespace=namespace)
        except PolicyRejected as exc:
            high = sum(
                risk.severity == "high" for risk in exc.result.remaining_risks
            )
            err.print(
                f"[red]context enforcement rejected output:[/] {high} high "
                f"risk(s) remain after {exc.result.passes} pass(es)"
            )
            raise typer.Exit(3)
        if policy.rejected:
            high = sum(
                risk.severity == "high" for risk in policy.remaining_risks
            )
            err.print(
                f"[red]context enforcement rejected output:[/] {high} high "
                f"risk(s) remain after {policy.passes} pass(es)"
            )
            raise typer.Exit(3)
        err.print(
            f"[green]context enforcement:[/] {len(policy.actions)} action(s), "
            f"{policy.passes} pass(es)"
        )
        selected = policy.entries

    if max_level is not None:
        selected = [
            entry for entry in selected
            if len((entry.display_path or entry.raw_path).parts) <= max_level
        ]
    mapped = transform_entries(selected, pseudo, progress=progress)
    if context_mode == "enforce" and rule_set.context_blur_structure:
        mapped = blur_mapped_structure(mapped)
    return mapped, policy


def _do_scan(
    path: Path,
    rules: Path | None,
    format: str,
    output: Path | None,
    no_ner: bool,
    no_org: bool,
    no_prof: bool,
    no_en: bool,
    paranoid: bool,
    no_size: bool,
    write_ignore: bool,
    db: Path | None,
    ner: str | None = None,
    recursive: int | None = None,
    no_pinyin: bool = False,
    no_identity: bool = False,
    llm: bool = False,
    llm_model: str | None = None,
    llm_url: str | None = None,
    jobs: int = 1,
    context_mode: str = "off",
) -> None:
    if not path.is_dir():
        err.print("[red]input is not an accessible directory[/]")
        raise typer.Exit(2)
    rule_set = _prepare_rules(rules, no_ner, no_org, no_prof, no_en, paranoid, ner, no_pinyin, no_identity, llm, llm_model, llm_url)
    # recursion depth: default = top level only; -r (0) = unlimited; -r N = N levels
    max_level = 1 if recursive is None else (None if recursive <= 0 else recursive)
    if rule_set.use_ner and rule_set.ner_engine != "off":
        from .detectors.hanlp_ner import available as _hanlp_ok

        active = {"auto": "hanlp+jieba" if _hanlp_ok() else "jieba"}.get(
            rule_set.ner_engine, rule_set.ner_engine
        )
        err.print(f"[dim]NER engine: {active}[/]")

    db_path = db or default_db_path()
    pseudo = Pseudonymizer(db_path)
    show_progress = err.is_interactive

    def _progress(msg: str) -> None:
        err.print(f"[dim]{msg}[/]", end="\r")

    try:
        mapped, _context = _context_mapped(
            path.resolve(), rule_set, pseudo,
            _exclusions_for(db_path, path.resolve()),
            _progress if show_progress else None,
            with_size=not no_size,
            max_level=max_level,
            jobs=jobs,
            context_mode=context_mode,
        )
        if show_progress:
            err.print("", end="\r")
        result = build_scan_result(path.resolve(), mapped, rule_set, pseudo,
                                   root_label="[ROOT]" if context_mode == "enforce" else None)
        if context_mode == "enforce" and rule_set.context_blur_stats:
            result = blur_scan_result(result)
        if write_ignore:
            n = _write_ignore_files(path.resolve(), mapped)
            err.print(f"[yellow]wrote .ignore into {n} real directorie(s)[/]")
    finally:
        pseudo.close()

    if format == "json":
        if output:
            with open(output, "w", encoding="utf-8") as f:
                write_json(result, f, include_size=not no_size)
            err.print(f"[green]wrote[/] {output}  ({stats_line(result)})")
        else:
            write_json(result, sys.stdout, include_size=not no_size)
    elif format == "tree":
        if len(result.entries) > 50_000:
            count = (
                bucket_count(len(result.entries))
                if context_mode == "enforce"
                else str(len(result.entries))
            )
            err.print(
                f"[red]{count} entries is too many for a tree; "
                "use -f json instead[/]"
            )
            raise typer.Exit(2)
        text = render_tree(result, show_size=not no_size,
                           width=None if output else console.size.width)
        if output:
            output.write_text(text + "\n", encoding="utf-8")
            err.print(f"[green]wrote[/] {output}  ({stats_line(result)})")
        else:
            # plain text: sanitized paths may contain '[' which rich would
            # otherwise parse as markup
            sys.stdout.write(text + "\n")
            err.print(f"[dim]{stats_line(result)}[/]")
    else:
        err.print(f"[red]unknown format:[/] {format}")
        raise typer.Exit(2)


def _do_analyze(
    path: Path,
    rules: Path | None,
    format: str,
    output: Path | None,
    k_anonymity: int | None,
    ner: str | None,
    llm: bool,
    llm_model: str | None,
    llm_url: str | None,
    jobs: int,
    db: Path | None,
) -> None:
    if not path.is_dir():
        err.print("[red]input is not an accessible directory[/]")
        raise typer.Exit(2)
    if format not in ("tree", "json"):
        err.print(f"[red]unknown format:[/] {format}")
        raise typer.Exit(2)
    if k_anonymity is not None and k_anonymity < 1:
        err.print("[red]--k-anonymity must be a positive integer[/]")
        raise typer.Exit(2)

    rule_set = _prepare_rules(
        rules, False, False, False, False, False, ner,
        llm=llm, llm_model=llm_model, llm_url=llm_url,
    )
    if not rule_set.context_enabled or rule_set.context_mode == "off":
        err.print("[red]context analysis is disabled by the rules file[/]")
        raise typer.Exit(2)

    root = path.resolve()
    pseudo = None
    db_path = db
    if rule_set.use_llm:
        db_path = db_path or default_db_path()
        pseudo = Pseudonymizer(db_path)
    exclude_paths = (
        _exclusions_for(db_path, root) if db_path is not None else frozenset()
    )
    try:
        # A fresh opaque namespace prevents report IDs from becoming stable
        # cross-release correlators or guessable unsalted path hashes.
        import secrets

        analysis_namespace = secrets.token_hex(32)
        # Context analysis always walks the complete visible tree. Sizes are
        # irrelevant to M2 rules, so disabling them avoids a second hidden-tree
        # traversal and keeps this command read-only and fast.
        raws, dir_sizes = walk_entries(
            root,
            rule_set,
            exclude_paths=exclude_paths,
            with_size=False,
            max_level=None,
            jobs=jobs,
        )
        detected = detect_entries(
            raws,
            rule_set,
            pseudo=pseudo,
            dir_sizes=dir_sizes,
            max_level=None,
            jobs=jobs,
            entry_namespace=analysis_namespace,
        )
        result = analyze_entries(
            detected,
            rule_set,
            namespace=analysis_namespace,
            k_anonymity=k_anonymity,
        )
    finally:
        if pseudo is not None:
            pseudo.close()

    text = analysis_to_json(result) if format == "json" else render_analysis(result)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        err.print(f"[green]wrote[/] {output}  ({len(result.risks)} risk(s))")
    else:
        sys.stdout.write(text + "\n")


@app.callback(invoke_without_command=True)
@_safe_errors
def main(ctx: typer.Context) -> None:
    """Bare `privacyfs` scans the current directory (same as `privacyfs scan .`)."""
    if ctx.invoked_subcommand is None:
        _do_scan(
            path=Path("."), rules=None, format="tree", output=None,
            no_ner=False, no_org=False, no_prof=False, no_en=False,
            paranoid=False, no_size=False, write_ignore=False, db=None,
            recursive=None,
        )


@app.command()
@_safe_errors
def scan(
    path: Path = typer.Argument(Path("."), help="Directory to scan (default: current directory)"),
    recursive: int | None = typer.Option(None, "--recursive", "-r", help="Descend into subdirectories: -r = unlimited, -r N = at most N levels (default: top level only)"),
    rules: Path | None = typer.Option(None, "--rules", help="YAML rules file (keep it OUTSIDE the scanned tree)"),
    format: str = typer.Option("tree", "--format", "-f", help="tree | json"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write to file instead of stdout"),
    no_ner: bool = typer.Option(False, "--no-ner", help="Disable NER person-name detection"),
    ner: str | None = typer.Option(None, "--ner", help="NER engine: auto|hanlp|jieba|off (hanlp needs pip install privacyfs[hanlp])"),
    no_org: bool = typer.Option(False, "--no-org", help="Disable organization detection"),
    no_prof: bool = typer.Option(False, "--no-professions", help="Disable profession/job-title masking"),
    no_en: bool = typer.Option(False, "--no-en-names", help="Disable English name detection"),
    no_pinyin: bool = typer.Option(False, "--no-pinyin", help="Disable pinyin name detection"),
    no_identity: bool = typer.Option(False, "--no-identity", help="Disable identity-implying term masking"),
    llm: bool = typer.Option(False, "--llm", help="Ask a local LLM (Ollama) about unflagged names"),
    llm_model: str | None = typer.Option(None, "--llm-model", help="Ollama model name (default: privacyfs-minicpm)"),
    llm_url: str | None = typer.Option(None, "--llm-url", help="Ollama base URL"),
    paranoid: bool = typer.Option(False, "--paranoid", help="Also mask QQ/bank-card-like numbers (noisy)"),
    no_size: bool = typer.Option(False, "--no-size", help="Omit file sizes from JSON output"),
    jobs: int = typer.Option(1, "--jobs", "-j", help="Worker count; default 1 = serial. -j 0 = auto (cpu_count)"),
    context_mode: str = typer.Option("off", "--context-mode", help="off | audit | enforce"),
    write_ignore: bool = typer.Option(False, "--write-ignore", help="Write .ignore files into the REAL tree (marks sensitive entries)"),
    db: Path | None = typer.Option(None, "--db", help="Mapping DB path (default: %LOCALAPPDATA%\\PrivacyFS)"),
) -> None:
    """Scan PATH and print an anonymized listing safe to paste to AI tools."""
    _do_scan(path, rules, format, output, no_ner, no_org, no_prof, no_en,
             paranoid, no_size, write_ignore, db, ner, recursive, no_pinyin, no_identity, llm, llm_model, llm_url, jobs, context_mode)


@app.command()
@_safe_errors
def analyze(
    path: Path = typer.Argument(Path("."), help="Directory to analyze recursively"),
    rules: Path | None = typer.Option(None, "--rules", help="YAML rules file"),
    format: str = typer.Option("tree", "--format", "-f", help="tree | json"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    k_anonymity: int | None = typer.Option(None, "--k-anonymity", "-k"),
    ner: str | None = typer.Option(None, "--ner", help="NER engine: auto|hanlp|jieba|off"),
    llm: bool = typer.Option(False, "--llm", help="Use local Ollama for unflagged names"),
    llm_model: str | None = typer.Option(None, "--llm-model"),
    llm_url: str | None = typer.Option(None, "--llm-url"),
    jobs: int = typer.Option(1, "--jobs", "-j"),
    db: Path | None = typer.Option(None, "--db", help="Optional LLM verdict cache DB"),
) -> None:
    """Audit multi-file and multi-directory inference risks without modifying PATH."""
    _do_analyze(
        path, rules, format, output, k_anonymity, ner,
        llm, llm_model, llm_url, jobs, db,
    )


@app.command()
@_safe_errors
def tui(
    path: Path = typer.Argument(Path("."), help="Directory to scan (default: current directory)"),
    recursive: int | None = typer.Option(None, "--recursive", "-r", help="-r = unlimited, -r N = N levels"),
    rules: Path | None = typer.Option(None, "--rules"),
    ner: str | None = typer.Option(None, "--ner", help="NER engine: auto|hanlp|jieba|off"),
    llm: bool = typer.Option(False, "--llm", help="Ask a local LLM (Ollama) about unflagged names"),
    llm_model: str | None = typer.Option(None, "--llm-model"),
    llm_url: str | None = typer.Option(None, "--llm-url"),
    jobs: int = typer.Option(1, "--jobs", "-j", help="Worker count; default 1 = serial. -j 0 = auto"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Interactive TUI browser: true column alignment at any terminal width,
    scrolling instead of wrapping. Sanitized names only."""
    import threading

    from .emit.tui import run_tui

    if not path.is_dir():
        err.print("[red]input is not an accessible directory[/]")
        raise typer.Exit(2)
    rule_set = _prepare_rules(rules, False, False, False, False, False, ner,
                              llm=llm, llm_model=llm_model, llm_url=llm_url)

    holder: dict = {"result": None, "msg": "准备中…", "cancel": threading.Event()}
    db_path = db or default_db_path()

    def worker() -> None:
        from .core import ScanCancelled

        pseudo = None
        try:
            pseudo = Pseudonymizer(db_path)
            max_level = 1 if recursive is None else (None if recursive <= 0 else recursive)
            mapped = iter_mapped(
                path.resolve(), rule_set, pseudo, _exclusions_for(db_path, path.resolve()),
                lambda m: holder.__setitem__("msg", m),
                with_size=True, max_level=max_level, jobs=jobs,
                cancel=holder["cancel"],
            )
            holder["result"] = to_result(path.resolve(), mapped, rule_set, pseudo)
        except ScanCancelled:
            holder["msg"] = "已取消扫描"
        except Exception as exc:  # surface worker errors in the UI
            holder["msg"] = f"扫描失败: {type(exc).__name__}"
            holder["result"] = holder.get("result")
        finally:
            if pseudo is not None:
                pseudo.close()

    threading.Thread(target=worker, daemon=True).start()
    run_tui(holder)


@app.command()
@_safe_errors
def mirror(
    path: Path = typer.Argument(..., help="Directory to mirror"),
    dest: Path = typer.Argument(..., help="Mirror destination (created; must be OUTSIDE the scanned tree)"),
    rules: Path | None = typer.Option(None, "--rules"),
    no_ner: bool = typer.Option(False, "--no-ner"),
    ner: str | None = typer.Option(None, "--ner", help="NER engine: auto|hanlp|jieba|off"),
    no_org: bool = typer.Option(False, "--no-org"),
    no_prof: bool = typer.Option(False, "--no-professions"),
    no_en: bool = typer.Option(False, "--no-en-names"),
    no_pinyin: bool = typer.Option(False, "--no-pinyin", help="Disable pinyin name detection"),
    no_identity: bool = typer.Option(False, "--no-identity", help="Disable identity-implying term masking"),
    llm: bool = typer.Option(False, "--llm", help="Ask a local LLM (Ollama) about unflagged names"),
    llm_model: str | None = typer.Option(None, "--llm-model"),
    llm_url: str | None = typer.Option(None, "--llm-url"),
    paranoid: bool = typer.Option(False, "--paranoid", help="Also mask QQ/bank-card-like numbers (noisy)"),
    db: Path | None = typer.Option(None, "--db"),
    copy: bool = typer.Option(False, "--copy", help="Copy instead of hardlink (write-isolated: safe to let AI modify files)"),
    jobs: int = typer.Option(1, "--jobs", "-j", help="Worker count; default 1 = serial. -j 0 = auto"),
    context_mode: str = typer.Option("off", "--context-mode", help="off | audit | enforce"),
    scrub: bool = typer.Option(False, "--scrub-metadata", help="Copy + strip embedded metadata (author/EXIF/PDF info) and flatten timestamps"),
    force: bool = typer.Option(False, "--force", help="Rebuild an existing mirror (only one we created)"),
) -> None:
    """Build an anonymized MIRROR of PATH at DEST.

    Point AI tools at DEST: every file and directory name is pseudonymized,
    hidden subtrees become empty [HIDDEN] placeholders. Files are hardlinks
    by default: CONTENTS are byte-identical to the originals and writes go
    through to them. Use --copy if the AI should be able to modify files.
    Use --scrub-metadata to also strip embedded metadata (Office author,
    PDF /Info, JPEG EXIF) and flatten timestamps -- implies real copies.
    """
    if not path.is_dir():
        err.print("[red]input is not an accessible directory[/]")
        raise typer.Exit(2)
    rule_set = _prepare_rules(rules, no_ner, no_org, no_prof, no_en, paranoid, ner, no_pinyin, no_identity, llm, llm_model, llm_url)

    db_path = db or default_db_path()
    pseudo = Pseudonymizer(db_path)
    show_progress = err.is_interactive

    def _mirror_progress(msg: str) -> None:
        err.print(f"[dim]{msg}[/]", end="\r")

    try:
        mapped, _context = _context_mapped(
            path.resolve(), rule_set, pseudo,
            _exclusions_for(db_path, path.resolve()),
            _mirror_progress if show_progress else None,
            with_size=False,
            max_level=None,
            jobs=jobs,
            context_mode=context_mode,
        )
        if show_progress:
            err.print("", end="\r")
        try:
            stats = build_mirror(
                path, mapped, dest, overwrite=force,
                force_copy=copy, scrub_metadata=scrub,
                blur_stats=(
                    context_mode == "enforce" and rule_set.context_blur_stats
                ),
            )
        except MirrorBuildError as exc:
            err.print(f"[red]{exc}[/]")
            raise typer.Exit(4)
        except OSError:
            err.print("[red]mirror filesystem operation failed; output not confirmed[/]")
            raise typer.Exit(4)
        except ValueError as exc:
            err.print(f"[red]{exc}[/]")
            raise typer.Exit(2)
    finally:
        pseudo.close()

    if context_mode == "enforce" and rule_set.context_blur_stats:
        counts = {
            "linked": bucket_count(stats.linked),
            "copied": bucket_count(stats.copied),
            "scrubbed": bucket_count(stats.scrubbed),
            "plain": bucket_count(stats.unscrubbed),
            "dirs": bucket_count(stats.dirs),
            "hidden": bucket_count(stats.hidden),
            "skipped": bucket_count(stats.skipped),
            "collisions": bucket_count(stats.collisions),
        }
    else:
        counts = {
            "linked": str(stats.linked), "copied": str(stats.copied),
            "scrubbed": str(stats.scrubbed), "plain": str(stats.unscrubbed),
            "dirs": str(stats.dirs), "hidden": str(stats.hidden),
            "skipped": str(stats.skipped), "collisions": str(stats.collisions),
        }
    err.print(
        f"[green]mirror ready[/] {dest}  "
        f"({counts['linked']} linked, {counts['copied']} copied, "
        f"{counts['scrubbed']} scrubbed, {counts['plain']} plain-copied, "
        f"{counts['dirs']} dirs, {counts['hidden']} hidden, "
        f"{counts['skipped']} skipped, {counts['collisions']} collisions)"
    )
    if stats.linked:
        err.print(
            "[yellow]warning: hardlinked files SHARE CONTENT with originals -- "
            "an AI writing through the mirror modifies your real files. "
            "Use --copy for write isolation.[/]"
        )


@map_app.command("show")
@_safe_errors
def map_show(
    db: Path | None = typer.Option(None, "--db"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write to file (keeps real names out of terminal scrollback)"),
) -> None:
    """Print the mapping table. SENSITIVE: this is the deanonymization key."""
    pseudo = Pseudonymizer(db or default_db_path())
    try:
        text = mappings_to_json(pseudo)
    finally:
        pseudo.close()
    if output:
        output.write_text(text, encoding="utf-8")
        err.print(f"[green]wrote[/] {output}  [yellow]keep this file private[/]")
    else:
        err.print("[yellow]warning: real names will appear in terminal scrollback; prefer -o FILE[/]")
        console.print(text, highlight=False)


@map_app.command("reset")
@_safe_errors
def map_reset(
    db: Path | None = typer.Option(None, "--db"),
    yes: bool = typer.Option(False, "--yes", help="Confirm deletion"),
) -> None:
    """Delete all aliases. Old anonymized outputs become undecipherable."""
    if not yes:
        err.print("[red]refusing without --yes[/]")
        raise typer.Exit(2)
    pseudo = Pseudonymizer(db or default_db_path())
    try:
        pseudo.reset()
    finally:
        pseudo.close()
    err.print("[green]mapping cleared[/]")


@app.command("inspect")
@_safe_errors
def inspect_files(
    paths: list[Path] = typer.Argument(..., help="Explicit TXT/Markdown/CSV files; no recursive body scan"),
    rules: Path | None = typer.Option(None, "--rules"),
    encoding: str | None = typer.Option(None, "--encoding", help="Default: BOM detection, otherwise strict UTF-8"),
    max_file_bytes: int = typer.Option(8 * 1024 * 1024, "--max-file-bytes", min=1),
    max_total_bytes: int = typer.Option(64 * 1024 * 1024, "--max-total-bytes", min=1),
    max_seconds: float = typer.Option(10.0, "--max-seconds", min=0.001),
) -> None:
    """D1 local rules inspection. JSON reports parsing coverage, not privacy certification."""
    import json
    import os
    from .documents import DocumentSession, Limits, ParseStatus, RuleInspector, public_report
    from .documents.models import ProcessingStopped

    limits = Limits(max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes, max_seconds=max_seconds)
    if len(paths) > limits.max_files:
        err.print("[red]too many explicitly selected files[/]")
        raise typer.Exit(2)
    rule_set = _prepare_rules(rules, True, False, False, False, False)
    absolute = [Path(os.path.abspath(path)) for path in paths]
    root = Path(os.path.commonpath([str(path.parent) for path in absolute]))
    incomplete = False
    try:
        inspector = RuleInspector(rule_set, limits=limits)
        with DocumentSession(root, limits=limits) as session:
            sys.stdout.write('{"schema_version": "d1-inspection-1", "documents": [\n')
            for index, path in enumerate(absolute):
                snapshot = session.capture(path.relative_to(root))
                document = session.parse(snapshot, encoding=encoding)
                reason, findings = None, []
                if document.coverage.status in {ParseStatus.COMPLETE, ParseStatus.PARTIAL}:
                    try:
                        findings = inspector.inspect(document)
                    except ProcessingStopped as exc:
                        reason = exc.reason
                report = public_report(document, findings, inspection_reason=reason)
                report["input_index"] = index
                if index:
                    sys.stdout.write(",\n")
                json.dump(report, sys.stdout, ensure_ascii=False)
                incomplete = incomplete or report["status"] != "complete"
                session.release(snapshot)
            sys.stdout.write("\n]}\n")
    except ProcessingStopped as exc:
        err.print(f"[red]document processing stopped: {exc.reason}[/]")
        raise typer.Exit(4) from None
    if incomplete:
        raise typer.Exit(4)


@app.command("relate")
@_safe_errors
def relate_files(
    root: Path = typer.Argument(..., help="Local project root"),
    config: Path = typer.Option(..., "--config", help="Explicit d2-templates-1 JSON file bindings"),
    quasi_field: list[str] | None = typer.Option(None, "--quasi-field", help="Repeat for at least two configured quasi fields"),
    max_support: int = typer.Option(2, "--max-support", min=1),
    max_seconds: float = typer.Option(10.0, "--max-seconds", min=0.001),
) -> None:
    """D2 explicit cross-file evidence. No source edits or privacy certification."""
    import json
    from .documents import DocumentSession, Limits
    from .documents.models import ProcessingStopped
    from .evidence import EvidenceGraph, GraphLimits, public_graph_report
    from .relations import load_relation_config

    bindings = load_relation_config(config)
    try:
        with DocumentSession(root, limits=Limits(max_seconds=max_seconds)) as session:
            parsed = [(session.parse(session.capture(path), encoding=encoding), template)
                      for path, template, encoding in bindings]
            with EvidenceGraph(parsed, limits=GraphLimits(max_seconds=max_seconds)) as graph:
                analysis = graph.analyze(quasi_fields=tuple(quasi_field or ()), max_support=max_support)
                report = public_graph_report(graph, analysis)
                # Construct and validate the whole public result before stdout.
                sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    except ProcessingStopped as exc:
        sys.stdout.write(json.dumps({"schema_version": "d2-evidence-1", "status": "failed",
            "reason": exc.reason, "risk_count": None, "privacy_verified": False}) + "\n")
        raise typer.Exit(4) from None


def _release_errors(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        import json
        from .verifier import ReleaseRejected
        from .documents.models import ProcessingStopped
        try:
            return function(*args, **kwargs)
        except ReleaseRejected as exc:
            sys.stdout.write(json.dumps({"schema_version": "d3-release-status-1", "state": "REJECTED",
                "reason": exc.reason, "privacy_verified": False}) + "\n")
            raise typer.Exit(3) from None
        except ProcessingStopped as exc:
            sys.stdout.write(json.dumps({"schema_version": "d3-release-status-1", "state": "FAILED",
                "reason": exc.reason, "privacy_verified": False}) + "\n")
            raise typer.Exit(4) from None
    return guarded


def _print_release(result):
    import json
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


@app.command("prepare")
@_safe_errors
@_release_errors
def prepare_command(
    root: Path = typer.Argument(...),
    relations: Path = typer.Option(..., "--relations"),
    task_profile: Path = typer.Option(..., "--task-profile"),
    state_dir: Path = typer.Option(..., "--state-dir"),
) -> None:
    """Prepare and validate a local plan without publishing it."""
    from .release import prepare_release
    _print_release(prepare_release(root, relations, task_profile, state_dir))


@app.command("review")
@_safe_errors
@_release_errors
def review_command(
    plan_id: str = typer.Argument(...),
    state_dir: Path = typer.Option(..., "--state-dir"),
    approve: str | None = typer.Option(None, "--approve", help="Explicit approval_digest from the reviewed plan"),
) -> None:
    """Read the safe plan, or approve its exact digest after fresh validation."""
    from .release import review_release
    _print_release(review_release(plan_id, state_dir, approve=approve))


@app.command("export")
@_safe_errors
@_release_errors
def export_command(
    plan_id: str = typer.Argument(...),
    destination: Path = typer.Argument(..., help="Container for a new neutral RELEASE_* directory"),
    state_dir: Path = typer.Option(..., "--state-dir"),
) -> None:
    """Publish approved bytes into a new directory; existing releases are retained."""
    from .release import export_release
    _print_release(export_release(plan_id, destination, state_dir))


@release_app.command("show")
@_safe_errors
@_release_errors
def release_show_command(identifier: str, state_dir: Path = typer.Option(..., "--state-dir")) -> None:
    """Read a plan/release and verify the published files when present."""
    from .release import show_release
    _print_release(show_release(identifier, state_dir))


@release_app.command("recover")
@_safe_errors
@_release_errors
def release_recover_command(plan_id: str, state_dir: Path = typer.Option(..., "--state-dir")) -> None:
    """Reconcile an interrupted switch using the sealed private journal."""
    from .release import recover_release
    _print_release(recover_release(plan_id, state_dir))


def _workspace_errors(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        import json
        from .verifier import ReleaseRejected
        from .documents.models import ProcessingStopped
        try:
            return function(*args, **kwargs)
        except (ReleaseRejected, ProcessingStopped) as exc:
            sys.stdout.write(json.dumps({"schema_version": "d4-workspace-report-1", "status": "failed",
                "reason": exc.reason, "risk_count": None, "privacy_verified": False}) + "\n")
            raise typer.Exit(4 if isinstance(exc, ProcessingStopped) else 3) from None
    return guarded


@workspace_app.command("init")
@_safe_errors
@_workspace_errors
def workspace_init(root: Path, relations: Path = typer.Option(..., "--relations"),
                   task_profile: Path = typer.Option(..., "--task-profile"), state_dir: Path = typer.Option(..., "--state-dir")):
    from .state_store import create_workspace
    from .review import refresh_workspace
    created = create_workspace(root, relations, task_profile, state_dir)
    _print_release(refresh_workspace(created["workspace_id"], state_dir))


@workspace_app.command("list")
@_safe_errors
@_workspace_errors
def workspace_list(state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import list_workspaces
    _print_release(list_workspaces(state_dir))


@workspace_app.command("refresh")
@_safe_errors
@_workspace_errors
def workspace_refresh(workspace_id: str, state_dir: Path = typer.Option(..., "--state-dir"), full: bool = typer.Option(False, "--full")):
    from .review import refresh_workspace
    _print_release(refresh_workspace(workspace_id, state_dir, force=full))


@workspace_app.command("status")
@_safe_errors
@_workspace_errors
def workspace_status_command(workspace_id: str, state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import workspace_status
    _print_release(workspace_status(workspace_id, state_dir))


@workspace_app.command("review")
@_safe_errors
@_workspace_errors
def workspace_review(workspace_id: str, state_dir: Path = typer.Option(..., "--state-dir"), local: bool = typer.Option(False, "--local")):
    from .review import refresh_workspace, run_local_console
    from .verifier import ReleaseRejected
    if local:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ReleaseRejected("LOCAL_TERMINAL_REQUIRED")
        run_local_console(workspace_id, state_dir)
    else:
        _print_release(refresh_workspace(workspace_id, state_dir))


@workspace_app.command("correct")
@_safe_errors
@_workspace_errors
def workspace_correct(workspace_id: str, action: str, targets: list[str] = typer.Argument(...),
                      expected_revision: int = typer.Option(..., "--expected-revision", min=0), state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import correct_workspace
    _print_release(correct_workspace(workspace_id, state_dir, action, targets, expected_revision=expected_revision))


@workspace_app.command("undo")
@_safe_errors
@_workspace_errors
def workspace_undo(workspace_id: str, expected_revision: int = typer.Option(..., "--expected-revision", min=0), state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import undo_workspace
    _print_release(undo_workspace(workspace_id, state_dir, expected_revision=expected_revision))


@workspace_app.command("configure")
@_safe_errors
@_workspace_errors
def workspace_configure(workspace_id: str, kind: str, file: Path,
                        expected_revision: int = typer.Option(..., "--expected-revision", min=0), state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import configure_workspace
    _print_release(configure_workspace(workspace_id, state_dir, kind, file, expected_revision=expected_revision))


@workspace_app.command("prepare")
@_safe_errors
@_workspace_errors
def workspace_prepare(workspace_id: str, state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import prepare_workspace
    _print_release(prepare_workspace(workspace_id, state_dir))


@workspace_app.command("prune")
@_safe_errors
@_workspace_errors
def workspace_prune(workspace_id: str, state_dir: Path = typer.Option(..., "--state-dir"),
                    apply: bool = typer.Option(False, "--apply"), expected_revision: int | None = typer.Option(None, "--expected-revision"),
                    retention: str | None = typer.Option(None, "--retention", help="current or all")):
    from .review import retention_workspace
    if retention not in {None, "current", "all"}:
        raise ValueError("invalid cache retention")
    _print_release(retention_workspace(workspace_id, state_dir, apply=apply, expected_revision=expected_revision,
                                     keep_cache=None if retention is None else retention == "all"))


@workspace_app.command("forget")
@_safe_errors
@_workspace_errors
def workspace_forget(workspace_id: str, expected_revision: int = typer.Option(..., "--expected-revision", min=0), state_dir: Path = typer.Option(..., "--state-dir")):
    from .review import forget_workspace
    _print_release(forget_workspace(workspace_id, state_dir, expected_revision=expected_revision))


@app.command("doctor")
@_safe_errors
def doctor_command(self_test: bool = typer.Option(False, "--self-test"), bundle: Path | None = typer.Option(None, "--bundle"),
                   state_dir: Path | None = typer.Option(None, "--state-dir")):
    """Safe installation diagnostics. Never includes customer files or state payloads."""
    from .diagnostics import doctor, write_diagnostic_bundle
    report = doctor(self_test=self_test, state_dir=state_dir)
    if bundle:
        write_diagnostic_bundle(bundle, report)
    _print_release(report)
    if report["self_test"] == "failed" or report.get("state", {}).get("status") == "not_readable":
        raise typer.Exit(4)


@app.command("sample")
@_safe_errors
def sample_command(destination: Path):
    """Create synthetic D5 trial materials in a new directory."""
    from .sample_documents import create_trial
    _print_release(create_trial(destination))


if __name__ == "__main__":
    entrypoint()
