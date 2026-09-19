"""Lazy llama.cpp binding owned by the GUI's spawned scan worker."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import multiprocessing
import os
from pathlib import Path
import time


class LocalModelError(Exception):
    """Only fixed, path-free codes may leave this module as public errors."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _load_llama():
    if multiprocessing.current_process().name == "MainProcess":
        raise LocalModelError("ai_worker_required")
    try:
        from llama_cpp import Llama
    except (ImportError, OSError, RuntimeError):
        raise LocalModelError("ai_dependency_missing") from None
    return Llama


def _configure_chat_template(model):
    """Use a GGUF template's explicit non-thinking branch when it has one.

    The Python chat API does not forward server chat_template_kwargs. Leaving
    this undefined can prefill <think> while JSON grammar is already active.
    """
    template = getattr(model, "metadata", {}).get("tokenizer.chat_template")
    if not isinstance(template, str) or "enable_thinking" not in template:
        return
    from llama_cpp.llama_chat_format import Jinja2ChatFormatter
    eos, bos = model.token_eos(), model.token_bos()
    def text(token):
        return model.detokenize([token], special=True).decode("utf-8", "replace") if token >= 0 else ""
    formatter = Jinja2ChatFormatter(
        template="{% set enable_thinking = false %}" + template,
        eos_token=text(eos), bos_token=text(bos), stop_token_ids=[eos] if eos >= 0 else None)
    model.chat_handler = formatter.to_chat_handler()
    return formatter


@contextmanager
def quiet_native():
    """Suppress native diagnostics that may contain model paths or prompts."""
    saved = []
    with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            for descriptor in (1, 2):
                try:
                    duplicate = os.dup(descriptor)
                except OSError:  # pythonw may have no standard descriptors.
                    continue
                saved.append((descriptor, duplicate))
                os.dup2(sink.fileno(), descriptor)
            yield
        finally:
            for descriptor, duplicate in reversed(saved):
                os.dup2(duplicate, descriptor)
                os.close(duplicate)


class EmbeddedModel:
    def __init__(self, options, cancel, notify):
        self.options, self.cancel, self.notify = options, cancel, notify
        self.model = None
        self.loads = 0

    def check(self, started):
        if self.cancel.is_set():
            raise InterruptedError("cancelled")
        if time.monotonic() - started >= self.options.ai_worker_timeout:
            raise LocalModelError("ai_worker_timeout")

    def load(self):
        if self.model is not None:
            return
        started = time.monotonic()
        self.notify(phase="正在加载本地 GGUF 模型", engine_state="loading", engine_started_at=started)
        self.check(started)
        path = Path(self.options.ai_model_path).expanduser().absolute()
        try:
            st = path.lstat()
            if path.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400 or not path.is_file():
                raise ValueError("invalid model file")
            with path.open("rb") as stream:
                if stream.read(4) != b"GGUF":
                    raise ValueError("invalid model header")
        except (OSError, ValueError):
            raise LocalModelError("ai_model_file_invalid") from None
        try:
            with quiet_native():
                model_class = _load_llama()
                self.model = model_class(model_path=str(path), n_ctx=self.options.ai_context,
                                         n_threads=self.options.ai_threads, n_gpu_layers=0,
                                         n_batch=256, use_mmap=True, verbose=False, seed=0)
                _configure_chat_template(self.model)
        except LocalModelError:
            raise
        except Exception:
            raise LocalModelError("ai_model_load_failed") from None
        self.loads += 1
        self.check(started)
        self.notify(phase="模型已加载", engine_state="idle", ai_model_loads=self.loads)

    def review(self, records):
        from .directory_ai import inventory_text, response_schema, parse_completion, SYSTEM_PROMPT, MAX_RESPONSE, MAX_OUTPUT_TOKENS
        tool_mode = self.options.workflow
        if tool_mode:
            from .mark_tools import TOOL_PROMPT, tool_schema, dispatch_tools
            SYSTEM_PROMPT, response_schema = TOOL_PROMPT, tool_schema
        self.load()
        started = time.monotonic()
        last_progress = started
        self.notify(phase="本地模型正在分析目录清单", engine_state="generating", engine_started_at=started)
        parts, length, finish, chunks = [], 0, None, None
        try:
            self.check(started)
            with quiet_native():
                chunks = self.model.create_chat_completion(
                    messages=[{"role":"system", "content":SYSTEM_PROMPT},
                              {"role":"user", "content":inventory_text(records)}],
                    response_format={"type":"json_object", "schema":response_schema(records)},
                    stream=True, temperature=0, max_tokens=min(MAX_OUTPUT_TOKENS, self.options.ai_context // 2))
                for chunk in chunks:
                    self.check(started)
                    now = time.monotonic()
                    if now - last_progress >= 1:
                        self.notify(phase=f"本地模型推理中（{now-started:.0f}s）")
                        last_progress = now
                    choices = chunk.get("choices", [])
                    if len(choices) != 1:
                        raise ValueError("invalid native reply")
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    if delta.get("tool_calls") or delta.get("function_call"):
                        raise ValueError("unexpected native tool call")
                    text = delta.get("content") or ""
                    if not isinstance(text, str):
                        raise ValueError("invalid native text")
                    length += len(text.encode("utf-8"))
                    if length > MAX_RESPONSE:
                        raise ValueError("native response limit")
                    parts.append(text)
                    if choice.get("finish_reason") is not None:
                        finish = choice["finish_reason"]
                self.check(started)
            if tool_mode:
                if finish != "stop":
                    raise ValueError("incomplete tool request")
                return dispatch_tools("".join(parts), records)
            return parse_completion({"choices":[{"finish_reason":finish,
                "message":{"content":"".join(parts)}}]}, records)
        finally:
            if chunks is not None and hasattr(chunks, "close"):
                with quiet_native():
                    chunks.close()
            self.notify(engine_state="idle")

    def close(self):
        model, self.model = self.model, None
        if model is not None:
            self.notify(phase="正在释放模型", engine_state="releasing", engine_started_at=time.monotonic())
            try:
                with quiet_native():
                    model.close()
            except Exception:
                raise LocalModelError("ai_model_release_failed") from None
            self.notify(engine_state="released")
