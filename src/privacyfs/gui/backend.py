"""Bounded local filename scan service for the desktop UI.

Reuses existing detectors and filesystem predicates, never reads source bodies,
allocates aliases, or opens the mapping database. Raw results stay local.
"""
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import fnmatch
import http.client
import json
import multiprocessing
import os
import queue
import time

from ..config import load_rules
from ..core import _detectors_for
from ..detectors.llm import LLMDetector, _PROMPT
from ..scanner import _is_reparse, has_private_state_marker, inside_private_state, winlong
from .results import ResultStore, DirectoryStack
from .embedded_ai import LocalModelError
from ..ntfs.native import NtfsUnavailable, NtfsCancelled

_CACHE_SIZE = 16_384
DEFAULT_AI_CONTEXT = 131_072
MAX_AI_CONTEXT = 131_072


@dataclass(frozen=True)
class ScanOptions:
    root: str
    rules_path: str = ""
    recursive: bool = True
    include_parents: bool = False
    use_excludes: bool = True
    ner: str = "off"
    use_llm: bool = False
    model: str = "privacyfs-minicpm"
    llm_max: int = 500
    max_entries: int | None = None
    max_results: int | None = None
    directory_ai: bool = False
    ai_port: int = 8080
    ai_model: str = "privacyfs-local"
    ai_batch_items: int = 64
    ai_input_bytes: int = 6000
    ai_timeout: int = 60
    ai_backend: str = "embedded"
    ai_model_path: str = ""
    ai_context: int = DEFAULT_AI_CONTEXT
    ai_threads: int = 4
    ai_worker_timeout: int = 300
    enumeration_mode: str = "auto"
    workflow: bool = False

    def __post_init__(self):
        if self.ner not in {"off", "jieba"} or not self.root:
            raise ValueError("invalid scan settings")
        for name in ("recursive", "include_parents", "use_excludes", "use_llm", "directory_ai", "workflow"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("invalid switch")
        for name, cap in (("llm_max", 10_000), ("max_entries", None), ("max_results", None)):
            value = getattr(self, name)
            if value is None and name != "llm_max":
                continue
            if type(value) is not int or value < 1 or (cap is not None and value > cap):
                raise ValueError("invalid limit")
        if not isinstance(self.model, str) or (self.use_llm and not self.model.strip()) or len(self.model) > 200:
            raise ValueError("invalid model")
        if self.directory_ai and self.use_llm:
            raise ValueError("select one AI mode")
        for name, minimum, maximum in (("ai_port", 1, 65535), ("ai_batch_items", 1, 128),
                                        ("ai_input_bytes", 512, 32768), ("ai_timeout", 1, 300)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid directory AI setting")
        if not isinstance(self.ai_model, str) or not self.ai_model.strip() or len(self.ai_model) > 200:
            raise ValueError("invalid directory AI model")
        if self.ai_backend not in {"embedded", "server"}:
            raise ValueError("invalid directory AI backend")
        if self.enumeration_mode not in {"auto", "walk", "ntfs"}:
            raise ValueError("invalid enumeration mode")
        for name, minimum, maximum in (("ai_context", 2048, MAX_AI_CONTEXT), ("ai_threads", 1, 64),
                                       ("ai_worker_timeout", 1, 3600)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid local inference setting")
        if self.directory_ai and self.ai_backend == "embedded":
            path = self.ai_model_path
            if (not isinstance(path, str) or not path.strip() or "://" in path
                    or path.startswith(("\\\\", "//")) or Path(path).suffix.lower() != ".gguf"):
                raise ValueError("select a local GGUF file")


def display_text(text):
    """Make control/bidi characters visible without modifying filesystem paths."""
    return "".join(f"\\u{ord(c):04x}" if ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff
                   or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" else c for c in str(text))


def review_names(names, model):
    """Fixed loopback transport, no redirects, credentials or model downloads."""
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=5)
    prompt = _PROMPT.format(names="\n".join(f"{i}. {name}" for i, name in enumerate(names, 1)))
    payload = {"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}],
               "options": {"temperature": 0, "num_predict": 8 * len(names) + 32}}
    if "qwen" in model.lower():
        payload["think"] = False
    try:
        connection.request("POST", "/api/chat", json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read(1_048_577)
        if response.status != 200 or len(data) > 1_048_576:
            raise ValueError("local model request failed")
        return LLMDetector.parse_response(json.loads(data)["message"]["content"], names)
    finally:
        connection.close()


def scan(options, cancel, emit, commands=None):
    started = time.monotonic()
    stats = Counter({k: 0 for k in ("visited", "checked", "files", "dirs", "hits", "errors", "excluded", "reparse", "protected",
                                    "llm_attempted", "llm_checked", "llm_unchecked", "llm_failed_batches",
                                    "ai_batches", "ai_attempted", "ai_checked", "ai_unchecked", "ai_failed_batches",
                                    "ai_oversized", "ai_uncertain", "ai_stopped")})
    status, reason = "complete", "finished"
    hits, batch = [], []
    cache = OrderedDict()
    # This name memory caps attempts, including failures; no cross-run NONE cache.
    attempted_names = set()
    verdict_cache = {}  # At most llm_max entries, survives the general name LRU.
    llm_disabled = False
    root = Path(options.root).expanduser().absolute()
    directory_reviewer = None
    inventory = None
    enumeration_backend, enumeration_fallback = "walk", None

    def update(kind="progress", **values):
        if options.directory_ai or options.workflow:
            stats["ai_unchecked"] = stats["visited"] - stats["ai_checked"]
        emit({"kind": kind, "stats": dict(stats), "elapsed": round(time.monotonic()-started, 1),
              "enumeration_backend":enumeration_backend, "enumeration_fallback":enumeration_fallback, **values})

    def index_progress(**values):
        for key, value in values.items():
            if key.startswith("ntfs_"):
                stats[key] = value
        update(phase=values.get("phase", "准备 NTFS 索引"))

    preliminary, preview_count = False, 0
    frozen = ResultStore() if options.workflow else None

    def flush():
        nonlocal batch, hits, llm_disabled, status, reason, preview_count
        if not batch or cancel.is_set():
            batch = []
            return
        if frozen is not None:
            first = preview_count if preliminary else len(frozen)
            records = [{"entry_id":f"E{first+i+1}", "relative_path":rel.as_posix(),
                        "name":rel.name, "is_dir":is_dir} for i,(rel,is_dir,depth) in enumerate(batch)]
            if preliminary:
                preview_count += len(records)
            else:
                frozen.extend(records)
            update("inventory", rows=records)
            batch = []
            return
        if directory_reviewer is not None:
            for rel, is_dir, _ in batch:
                directory_reviewer.add(rel, is_dir)
                if directory_reviewer.limit_reached:
                    status, reason = "partial", "result_limit"
                    break
                if cancel.is_set():
                    break
            batch = []
            update(phase="目录 AI 已暂停" if directory_reviewer.disabled else "收集目录上下文")
            return
        names = sorted({name for rel, _, _ in batch for name in rel.parts})
        missing = [name for name in names if name not in cache]
        for name in missing:
            found = []
            for detector in detectors:
                found.extend((f.category, f.surface, type(detector).__name__) for f in detector.find(name))
            cache[name] = verdict_cache.get(name, (tuple(dict.fromkeys(found)), False))
        unmatched = [name for name in missing if not cache[name][0]]
        if options.use_llm:
            todo = [name for name in unmatched if name not in attempted_names]
            todo = todo[:max(0, options.llm_max - len(attempted_names))] if not llm_disabled else []
            for i in range(0, len(todo), 20):
                if cancel.is_set():
                    batch = []
                    return
                chunk = todo[i:i+20]
                attempted_names.update(chunk)
                stats["llm_attempted"] += len(chunk)
                try:
                    verdicts = review_names(chunk, options.model)
                except Exception:
                    verdicts = {}
                    stats["llm_failed_batches"] += 1
                    if stats["llm_failed_batches"] >= 2:
                        llm_disabled = True
                for name, label in verdicts.items():
                    if name not in chunk:
                        continue
                    stats["llm_checked"] += 1
                    category = {"PERSON":"PERSON", "ORG":"ORG", "IDENTITY":"IDENTITY", "ADULT":"KEYWORD"}.get(label)
                    cache[name] = (((category, name, "LocalLLM"),) if category else (), label == "NONE" or category is not None)
                    verdict_cache[name] = cache[name]
                update(phase="本地模型复核")
                if llm_disabled:
                    break
        for rel, is_dir, depth in batch:
            stats["checked"] += 1
            selected_parts = enumerate(rel.parts) if options.include_parents else [(len(rel.parts)-1, rel.name)]
            signals = []
            for index, name in selected_parts:
                found, checked = cache[name]
                if options.use_llm and not found and not checked:
                    stats["llm_unchecked"] += 1
                signals.extend({"component":name,"category":category,"surface":surface,"source":source,
                                "is_self":index == len(rel.parts)-1} for category,surface,source in found)
            if signals:
                stats["hits"] += 1
                hits.append({"path":str(root / rel),"relative_path":str(rel),"name":rel.name,"is_dir":is_dir,
                             "attribution":"self" if any(s["is_self"] for s in signals) else "parent", "signals":signals})
                if options.max_results is not None and stats["hits"] >= options.max_results:
                    status, reason = "partial", "result_limit"
                    break
        if hits:
            update("hits", rows=hits)
        hits, batch = [], []
        # Bound caches independently of full drive size.
        while len(cache) > _CACHE_SIZE:
            cache.popitem(last=False)
        update(phase="扫描名称")

    stack = None
    try:
        st = root.lstat()
        if root.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400 or not root.is_dir():
            raise ValueError("invalid root")
        if inside_private_state(root):
            raise ValueError("private state root")
        rules = load_rules(options.rules_path or None)
        rules.use_llm = False  # The UI's explicit switch is the only authorization.
        rules.use_ner, rules.ner_engine = options.ner != "off", options.ner
        if options.directory_ai and not options.workflow:
            from .directory_ai import DirectoryReviewer
            directory_reviewer = DirectoryReviewer(root, options, cancel, stats, update)
            detectors = []
        else:
            detectors = [] if options.workflow else _detectors_for(rules)
        exclusions = rules.excludes if options.use_excludes else []
        from ..ntfs.index import try_inventory
        if options.enumeration_mode == "ntfs" or options.enumeration_mode == "auto" and options.max_entries is None:
            enumeration_backend = "ntfs_preparing"
            try:
                if options.enumeration_mode == "ntfs":
                    inventory = try_inventory(root, options.recursive, exclusions, cancel, index_progress, allow_subdirectory=True, preliminary=options.workflow)
                elif options.workflow:
                    inventory = try_inventory(root, options.recursive, exclusions, cancel, index_progress, preliminary=True)
                else:
                    inventory = try_inventory(root, options.recursive, exclusions, cancel, index_progress)
            except NtfsUnavailable as error:
                if options.enumeration_mode == "ntfs":
                    enumeration_backend = "ntfs_unavailable"
                    enumeration_fallback = error.code
                    raise
                enumeration_backend = "walk"
                enumeration_fallback = error.code
            if inventory is not None:
                enumeration_backend = "ntfs"
                preliminary = bool(getattr(inventory, "preliminary", False))
                for key, value in inventory.stats.items():
                    stats[key] = value
        if inventory is not None:
            update(phase="显示未核验的 NTFS 名称预览" if preliminary else "使用 NTFS 名称清单",
                   inventory_verified=not preliminary)
            if getattr(inventory, "lazy", False):
                update("lazy_inventory", total_records=stats["ntfs_records"],
                       root_directory_id=str(inventory.selected_id), lazy_preview=True)
            elif preliminary and hasattr(inventory, "rows"):
                records = []
                for row in inventory.rows():
                    if cancel.is_set():
                        break
                    preview_count += 1
                    stats["visited"] += 1
                    stats["dirs" if row["is_dir"] else "files"] += 1
                    records.append({**row, "entry_id":f"E{preview_count}"})
                    if len(records) >= 512:
                        update("inventory", rows=records)
                        records = []
                if records:
                    update("inventory", rows=records)
            else:
                for rel, is_dir, depth in inventory.entries():
                    if cancel.is_set() or reason != "finished":
                        break
                    stats["visited"] += 1
                    stats["dirs" if is_dir else "files"] += 1
                    batch.append((rel,is_dir,depth))
                    if len(batch) >= (512 if preliminary else 128):
                        flush()
        else:
            stack = DirectoryStack()
            stack.append(("", 0))
            update(phase="普通目录遍历")
        while stack and not cancel.is_set() and reason == "finished":
            relative, depth = stack.pop()
            parent = Path(relative)
            folder = root / parent
            try:
                if has_private_state_marker(folder):
                    stats["protected"] += 1
                    continue
                # Re-check the directory itself before opening; no hostile-race sandbox claim.
                current = folder.lstat()
                if folder.is_symlink() or getattr(current, "st_file_attributes", 0) & 0x400:
                    stats["reparse"] += 1
                    continue
                with os.scandir(winlong(folder)) as iterator:
                    for child in iterator:
                        if cancel.is_set():
                            break
                        if options.max_entries is not None and stats["visited"] >= options.max_entries:
                            status, reason = "partial", "entry_limit"
                            break
                        if any(fnmatch.fnmatchcase(child.name.casefold(), p.casefold()) for p in exclusions):
                            stats["excluded"] += 1
                            continue
                        try:
                            if _is_reparse(child):
                                stats["reparse"] += 1
                                continue
                            is_dir = child.is_dir(follow_symlinks=False)
                            if is_dir and has_private_state_marker(Path(child.path)):
                                stats["protected"] += 1
                                continue
                        except OSError:
                            stats["errors"] += 1
                            continue
                        rel = parent / child.name
                        stats["visited"] += 1
                        stats["dirs" if is_dir else "files"] += 1
                        batch.append((rel, is_dir, depth))
                        if is_dir and options.recursive:
                            stack.append((str(rel), depth+1))
                        if len(batch) >= 128:
                            flush()
                            if reason != "finished":
                                break
            except OSError:
                stats["errors"] += 1
        if reason != "result_limit":
            flush()
        if frozen is not None and not cancel.is_set():
            if inventory is not None and not getattr(inventory, "lazy", False):
                inventory.close()
                inventory = None
            if stack is not None:
                stack.close()
                stack = None
            update(stage="preview", phase="名称索引已就绪；展开目录时读取当前页" if getattr(inventory,"lazy",False) else "目录结构已就绪；点击开始 AI 审阅",
                   inventory_complete=not preliminary and reason == "finished" and not bool(stats["errors"]),
                   inventory_verified=not preliminary, inventory_finished=True,
                   phase_detail="仅主名称预览；尚未补齐硬链接、核验访问范围或合并变动" if preliminary else "已核验")
            while not cancel.is_set():
                try:
                    selected = commands.get(timeout=0.1)
                    if isinstance(selected, dict) and selected.get("kind") == "browse":
                        if inventory is None or not getattr(inventory, "lazy", False):
                            continue
                        from ..ntfs.browser import browse
                        try:
                            page = browse(inventory, selected["parent"], selected["after"],
                                          selected["limit"], selected.get("directory_id"), cancel)
                        except NtfsCancelled:
                            raise
                        except Exception:
                            page = {"rows":[],"cursor":selected.get("after",0),"pending":False,"error":"directory_page_unavailable"}
                        stats["preview_pages"] += 1
                        update("directory_page", page_key=selected["key"], page=page)
                        continue
                    break
                except queue.Empty:
                    continue
            if not cancel.is_set():
                if (not isinstance(selected, ScanOptions) or not selected.directory_ai or not selected.workflow
                        or selected.ai_backend != "embedded" or selected.root != options.root):
                    raise ValueError("invalid review command")
                options = selected
                if preliminary:
                    reusable = inventory if getattr(inventory,"lazy",False) else None
                    if inventory is not None and reusable is None:
                        inventory.close()
                        inventory = None
                    enumeration_backend = "ntfs_preparing"
                    for key in list(stats):
                        if key.startswith("ntfs_"):
                            stats[key] = 0
                    update(stage="verifying", phase="正在核验预览；完成后才启动 AI", inventory_verified=False)
                    if reusable is not None:
                        inventory = try_inventory(root, options.recursive, exclusions, cancel, index_progress,
                                                  allow_subdirectory=True, existing=reusable)
                    else:
                        inventory = try_inventory(root, options.recursive, exclusions, cancel, index_progress,
                                                  allow_subdirectory=True)
                    if cancel.is_set():
                        raise NtfsCancelled()
                    preliminary = False
                    enumeration_backend = "ntfs"
                    for key, value in inventory.stats.items():
                        stats[key] = value
                    stats["visited"] = stats["files"] = stats["dirs"] = 0
                    update("inventory_reset", phase="核验完成，更新目录结构")
                    for rel, is_dir, depth in inventory.entries():
                        if cancel.is_set():
                            break
                        stats["visited"] += 1
                        stats["dirs" if is_dir else "files"] += 1
                        batch.append((rel,is_dir,depth))
                        if len(batch) >= 128:
                            flush()
                    flush()
                    inventory.close()
                    inventory = None
                    update(inventory_verified=True, inventory_complete=not bool(stats["errors"]), phase_detail="已核验")
                from .directory_ai import DirectoryReviewer
                directory_reviewer = DirectoryReviewer(root, options, cancel, stats, update)
                update(stage="review", phase="开始审阅已展示的目录清单")
                for item in frozen:
                    if cancel.is_set() or directory_reviewer.disabled:
                        break
                    directory_reviewer.add(Path(item["relative_path"]), item["is_dir"])
        if directory_reviewer is not None:
            directory_reviewer.finish()
            if directory_reviewer.limit_reached:
                status, reason = "partial", "result_limit"
        if cancel.is_set():
            status, reason = "cancelled", "cancelled"
        elif reason == "finished" and (stats["errors"] or stats["llm_unchecked"] or stats["ai_unchecked"]):
            status, reason = "partial", "coverage"
    except NtfsUnavailable as error:
        status, reason = "failed", "ntfs_" + error.code
    except LocalModelError as error:
        status, reason = "failed", error.code
    except Exception:
        status, reason = "failed", "scan_failed"
    finally:
        if frozen is not None:
            frozen.close()
        if inventory is not None:
            inventory.close()
        if directory_reviewer is not None:
            try:
                directory_reviewer.close()
            except LocalModelError as error:
                status, reason = "failed", error.code
        if stack is not None:
            stack.close()
    if cancel.is_set():
        status, reason = "cancelled", "cancelled"
    update("finished", status=status, reason=reason, privacy_verified=False)


def _worker(options, cancel, events, commands=None):
    def emit(value):
        while not cancel.is_set() or value["kind"] == "finished":
            try:
                events.put(value, timeout=0.1)
                return
            except queue.Full:
                if cancel.is_set():
                    return
    if options.workflow or options.directory_ai and options.ai_backend == "embedded":
        import gc
        from .embedded_ai import quiet_native
        with quiet_native():
            try:
                scan(options, cancel, emit, commands)
            finally:
                # Failed native constructors can retain traceback cycles;
                # collect them while private native diagnostics are suppressed.
                gc.collect()
    else:
        scan(options, cancel, emit, commands)


class ScanController:
    """Only the UI thread consumes results. Child processes never touch Slint."""
    def __init__(self, store_factory=ResultStore):
        self.store_factory = store_factory
        self.process = None
        self.events = None
        self.cancel_event = None
        self.rows = self.store_factory()
        self.report = {}
        self.options = None
        self.cancel_at = None
        self.dead_at = None
        self.finished = False
        self.ai_deadline = None
        self.commands = None
        self.inventory = None

    @property
    def running(self):
        return self.process is not None

    def start(self, options):
        if self.running:
            raise ValueError("scan already running")
        if self.inventory is not None:
            self.inventory.close()
            self.inventory = None
        if options.workflow:
            from ..workbench.tree_store import PreviewTreeStore
            self.inventory = PreviewTreeStore()
        context = multiprocessing.get_context("spawn")
        rows = self.store_factory()
        self.rows.close()
        self.rows, self.options, self.cancel_at, self.dead_at, self.finished = rows, options, None, None, False
        self.ai_deadline = None
        self.report = {"schema_version":"privacyfs-local-name-review-1", "root":options.root,
                       "status":"running", "privacy_verified":False, "stats":{},
                       "started_at":datetime.now(timezone.utc).isoformat(),
                       "coverage":"filename_only_accessible_entries", "llm_enabled":options.use_llm,
                       "directory_ai_enabled":options.directory_ai,
                       "tool_protocol":"privacyfs-mark-tools-1" if options.workflow else None,
                       "settings": {"rules_file":options.rules_path,"recursive":options.recursive,
                           "include_parents":options.include_parents,"use_excludes":options.use_excludes,
                           "ner":options.ner,"model":options.model if options.use_llm else None,
                           "llm_max":options.llm_max,"max_entries":options.max_entries,"max_results":options.max_results,
                           "directory_ai":options.directory_ai, "ai_backend":options.ai_backend if options.directory_ai else None,
                           "ai_port":options.ai_port, "ai_model":options.ai_model if options.directory_ai else None,
                           "ai_model_path":options.ai_model_path if options.directory_ai else None,
                           "ai_context":options.ai_context, "ai_threads":options.ai_threads,
                           "ai_worker_timeout":options.ai_worker_timeout,
                           "enumeration_mode":options.enumeration_mode,
                           "ai_batch_items":options.ai_batch_items, "ai_input_bytes":options.ai_input_bytes,
                           "ai_timeout":options.ai_timeout}}
        self.cancel_event = context.Event()
        self.events = context.Queue(maxsize=32)
        self.commands = context.Queue(maxsize=8) if options.workflow else None
        args = (options, self.cancel_event, self.events)
        if options.workflow:
            args += (self.commands,)
        self.process = context.Process(target=_worker, args=args, daemon=True)
        try:
            self.process.start()
        except Exception:
            self._release()
            self.report["status"] = "failed"
            raise

    def begin_review(self, options):
        if not self.running or self.report.get("stage") != "preview" or self.cancel_at is not None:
            raise ValueError("inventory not ready")
        if (not options.directory_ai or not options.workflow or options.ai_backend != "embedded"
                or options.root != self.options.root):
            raise ValueError("invalid review options")
        self.commands.put_nowait(options)
        if getattr(self.inventory, "lazy", False):
            self.inventory.pause()
        self.options = options
        self.report.update(stage="review", directory_ai_enabled=True)
        self.report["settings"].update(ai_model_path=options.ai_model_path, ai_context=options.ai_context,
            ai_threads=options.ai_threads, ai_batch_items=options.ai_batch_items,
            ai_input_bytes=options.ai_input_bytes, ai_worker_timeout=options.ai_worker_timeout,
            directory_ai=True, ai_backend=options.ai_backend)

    def request_page(self, command):
        if not self.running or self.cancel_at is not None or self.commands is None:
            return False
        try:
            self.commands.put_nowait(command)
            return True
        except queue.Full:
            return False

    def cancel(self):
        if self.running and self.cancel_at is None:
            self.cancel_at = time.monotonic()
            self.cancel_event.set()
            self.report["status"] = "cancelling"

    def poll(self):
        changed = False
        if not self.running:
            return changed
        deadline = time.monotonic() + 0.03
        for _ in range(64):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            changed = True
            if event["kind"] == "lazy_inventory":
                from ..workbench.remote_tree import RemoteTreeStore
                self.inventory.close()
                self.inventory = RemoteTreeStore(self.request_page, event["total_records"], event["root_directory_id"])
                self.report["coverage"] = "unverified_ntfs_name_index"
            if event["kind"] == "directory_page" and getattr(self.inventory, "lazy", False):
                self.inventory.accept(event["page_key"], event["page"])
            if event["kind"] == "inventory_reset":
                self.report["lazy_preview"] = False
                self.report["coverage"] = "filename_only_accessible_entries"
                from ..workbench.tree_store import PreviewTreeStore
                replacement = PreviewTreeStore()
                self.inventory.close()
                self.inventory = replacement
            if event["kind"] in {"inventory", "marks"}:
                try:
                    if event["kind"] == "inventory":
                        self.inventory.extend(event["rows"])
                        self.report["inventory_storage"] = "memory_and_protected_disk" if self.inventory.spilled else "memory"
                    else:
                        self.inventory.mark(event["decisions"])
                except Exception:
                    self.cancel_event.set()
                    self._release()
                    self.report.update(status="failed", reason="result_storage_failed")
                    return True
            if event["kind"] == "hits":
                try:
                    self.rows.extend(event["rows"])
                except Exception:
                    self.cancel_event.set()
                    self._release()
                    self.report.update(status="failed", reason="result_storage_failed",
                                       finished_at=datetime.now(timezone.utc).isoformat())
                    self.finished = True
                    return True
            self.report.update({k:v for k,v in event.items() if k not in {"kind", "rows", "decisions", "page", "page_key", "root_directory_id"}})
            if event.get("engine_state") in {"loading", "generating", "releasing"}:
                self.ai_deadline = event["engine_started_at"] + self.options.ai_worker_timeout
            elif event.get("engine_state") in {"idle", "released"}:
                self.ai_deadline = None
            if event["kind"] == "finished":
                self.finished = True
            if time.monotonic() >= deadline:
                break
        alive = self.process.is_alive()
        if alive and self.cancel_at is None and self.ai_deadline is not None and time.monotonic() >= self.ai_deadline:
            self.process.terminate()
            self.report.update(status="failed", reason="ai_worker_timeout",
                               finished_at=datetime.now(timezone.utc).isoformat())
            self.finished = True
            self._release()
            return True
        if self.cancel_at is not None and time.monotonic()-self.cancel_at > 2 and alive:
            self.process.terminate()
            self.report.update(status="cancelled", reason="terminated_after_cancel")
            self.finished = True
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            # A terminated Queue writer may leave a partial frame. Never read it again.
            self._release()
            return True
        if not alive:
            # A bounded poll may leave already-delivered events in the queue.
            # Only time out after delivery has stopped, not while draining it.
            if self.dead_at is None or changed:
                self.dead_at = time.monotonic()
            # Queue feeder delivery may lag process exit; don't invent an empty success.
            if self.finished or (not changed and time.monotonic()-self.dead_at > 0.5):
                if not self.finished:
                    self.report.update(status="cancelled" if self.cancel_at else "failed", reason="worker_exit")
                self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
                self._release()
                changed = True
        return changed

    def _release(self):
        self.ai_deadline = None
        if getattr(self.inventory, "lazy", False):
            self.inventory.pause()
        if self.commands is not None:
            self.commands.cancel_join_thread()
            self.commands.close()
            self.commands = None
        if self.process is not None:
            if self.process.pid is not None:
                self.process.join(timeout=0.1)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=1)
            self.process.close()
            self.process = None
        if self.events is not None:
            self.events.cancel_join_thread()
            self.events.close()
            self.events = None

    def close(self):
        if self.running:
            self.cancel_event.set()
            self._release()
        self.rows.close()
        if self.inventory is not None:
            self.inventory.close()

    def snapshot(self):
        if self.running:
            raise ValueError("scan still running")
        return {**self.report, "retained_hits":len(self.rows), "entries": iter(self.rows)}


def export_report(path, report):
    """Explicit local export; never overwrite an existing source or report."""
    with Path(path).open("x", encoding="utf-8") as stream:
        # Stream the entries instead of materializing the entire report in RAM.
        metadata = {k:v for k,v in report.items() if k != "entries"}
        stream.write(json.dumps(metadata, ensure_ascii=True)[:-1])
        stream.write(', "entries": [' if metadata else '"entries": [')
        first = True
        for row in report.get("entries", ()):
            if not first:
                stream.write(",")
            stream.write("\n")
            json.dump(row, stream, ensure_ascii=True)
            first = False
        stream.write("\n]}\n")
