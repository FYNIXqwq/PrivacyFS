"""Local-LLM detector via Ollama (default model: privacyfs-qwen, a Qwen
0.8B Q4 GGUF). Runs as a LATE batch stage: only components that no other
detector flagged are sent to the model, and verdicts are cached persistently
in the mapping DB so repeat scans cost nothing.

The LLM's judgments are noisy (0.8B!): false positives are harmless here
(over-masking is the safe direction), and false negatives are covered by the
rule/NER layers underneath. Treat this layer as a recall booster, never as
the only line of defense.
"""

from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import urllib.request

from .keywords import Finding

_LABELS = ("PERSON", "ORG", "IDENTITY", "ADULT", "NONE")
_CATEGORY = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "IDENTITY": "IDENTITY",
    "ADULT": "KEYWORD",
}
_LINE = re.compile(
    r"(\d+)\s*[.、:：]?\s*(?:类别\s*[:：])?\s*(PERSON|ORG|IDENTITY|ADULT|NONE)\b",
    re.IGNORECASE,
)

_PROMPT = """任务：逐个判断文件名是否泄露隐私。
规则：
- 含真实姓名 → PERSON
- 含公司/学校/机构名 → ORG
- 含能暴露身份的词（如奖学金、病历、职称、政审、成绩单）→ IDENTITY
- 成人内容 → ADULT
- 以上都不含 → NONE

例子：
工资表.xlsx → IDENTITY
旅游照片.jpg → NONE
李四合同.pdf → PERSON
华为采购.pdf → ORG

现在判断（每行只回答一个词）：
{names}"""


class LLMDetector:
    """find_batch(parts) -> {part: [Finding]}. `late = True` marks it for the
    post-rules stage in core.iter_mapped."""

    late = True

    def __init__(self, model: str = "privacyfs-minicpm",
                 url: str = "http://127.0.0.1:11434",
                 cache=None, batch_size: int = 20, timeout: int = 60,
                 max_items: int = 500, revision: str = "1"):
        self.model = model
        self.url = url.rstrip("/")
        namespace = hashlib.sha256(json.dumps(
            [self.url, model, revision, _PROMPT, _LABELS, "parser-1"], ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        self.cache = cache.llm_cache_for(namespace) if hasattr(cache, "llm_cache_for") else cache
        self.batch_size = batch_size
        self.timeout = timeout
        self.max_items = max_items
        self._broken = False
        self.progress = None        # optional callable(msg)
        self.cancel = None          # optional threading.Event

    # -- internals -----------------------------------------------------

    def _chat(self, prompt: str, batch_size: int) -> str:
        payload: dict = {
            "model": self.model,
            "stream": False,
            # cap generation: labels are a few tokens per name; without this a
            # derailed model can generate forever and look hung
            "options": {"temperature": 0, "num_predict": 8 * batch_size + 32},
            # keep the model resident between batches (default unloads after
            # 5 min idle, which stalls a later batch for tens of seconds)
            "keep_alive": "30m",
            "messages": [{"role": "user", "content": prompt}],
        }
        # only Qwen3.x needs reasoning suppression; other templates misbehave
        # (MiniCPM drops the line numbering when think is suppressed)
        if "qwen" in self.model.lower():
            payload["think"] = False
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["message"]["content"]

    @staticmethod
    def parse_response(text: str, chunk: list[str]) -> dict[str, str]:
        """Tolerate format drift: find 'number ... LABEL' anywhere per line.
        Some models drop the numbering entirely -- then fall back to matching
        bare labels positionally when the line count matches."""
        verdicts: dict[str, str] = {}
        for m in _LINE.finditer(text):
            idx = int(m.group(1)) - 1
            label = m.group(2).upper()
            if 0 <= idx < len(chunk) and chunk[idx] not in verdicts:
                verdicts[chunk[idx]] = label
        if not verdicts:
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) == len(chunk):
                for line, part in zip(lines, chunk):
                    m = re.search(r"\b(PERSON|ORG|IDENTITY|ADULT|NONE)\b", line,
                                  re.IGNORECASE)
                    if m:
                        verdicts[part] = m.group(1).upper()
        return verdicts

    # -- public --------------------------------------------------------

    def find_batch(self, parts: list[str]) -> dict[str, list[Finding]]:
        if self._broken or not parts:
            return {}
        out: dict[str, list[Finding]] = {}
        todo = list(dict.fromkeys(parts))

        cached: dict[str, str] = {}
        if self.cache is not None:
            cached = self.cache.llm_cache_get(todo)
            todo = [p for p in todo if p not in cached]
        todo = todo[: self.max_items]

        new_verdicts: dict[str, str] = {}
        failures = 0
        total = len(todo)
        t0 = time.monotonic()
        for i in range(0, total, self.batch_size):
            if self.cancel is not None and self.cancel.is_set():
                break
            chunk = todo[i : i + self.batch_size]
            numbered = "\n".join(f"{n}. {p}" for n, p in enumerate(chunk, 1))
            try:
                text = self._chat(_PROMPT.format(names=numbered), len(chunk))
            except Exception as exc:
                # one stalled/failed batch must not kill the whole layer:
                # skip it and carry on; two in a row means the server is gone
                failures += 1
                print(f"[privacyfs] LLM batch failed ({type(exc).__name__}), "
                      "skipping", file=sys.stderr)
                if failures >= 2:
                    self._broken = True
                    print("[privacyfs] LLM unavailable, continuing without it",
                          file=sys.stderr)
                    break
                continue
            failures = 0
            new_verdicts.update(self.parse_response(text, chunk))
            if self.progress is not None:
                elapsed = time.monotonic() - t0
                self.progress(
                    f"{min(i + self.batch_size, total)}/{total} 已用 {elapsed:.0f}s"
                )

        if self.cache is not None and new_verdicts:
            # Persist only verdicts that were actually parsed from a model
            # response. Failed, cancelled, or unparsed items must be retried
            # on the next scan instead of becoming permanent false negatives.
            self.cache.llm_cache_put(new_verdicts)
        # Unparsed names count as NONE for this run but are deliberately kept
        # separate from the persistent verdict set above.
        run_verdicts = dict(new_verdicts)
        for p in todo:
            run_verdicts.setdefault(p, "NONE")

        for part, label in {**cached, **run_verdicts}.items():
            if label in _CATEGORY:
                # the LLM can't localize spans, so mask the whole stem but
                # keep the file extension readable
                surface = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", part) or part
                out[part] = [Finding(_CATEGORY[label], surface)]
        return out

    def find(self, name: str) -> list[Finding]:
        return self.find_batch([name]).get(name, [])
