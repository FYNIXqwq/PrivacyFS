"""HanLP multi-task NER detector (optional dependency).

Uses the Electra-small multi-task model and unions its three NER corpora
(MSRA / PKU / OntoNotes): different corpora catch different names, and the
union meaningfully improves recall on context-free filename strings.

The model (~114MB) downloads once on first use and is cached. If HanLP's
built-in downloader is too slow, fetch the zip with curl and unzip it under
~/.hanlp/mtl/ (see README).
"""

from __future__ import annotations

import re
import sys

from .keywords import Finding

_CJK = re.compile(r"[一-鿿]")
# NER sometimes absorbs adjacent punctuation into the surface ("欧阳娜娜-")
_STRIP = re.compile(r"^[^0-9A-Za-z一-鿿]+|[^0-9A-Za-z一-鿿]+$")

_model = None
_broken = False

_CATEGORY = {
    "PERSON": "PERSON", "NR": "PERSON",
    "ORG": "ORG", "NT": "ORG", "ORGANIZATION": "ORG",
    "LOC": "LOCATION", "NS": "LOCATION", "LOCATION": "LOCATION", "GPE": "LOCATION",
}


def available() -> bool:
    try:
        import hanlp  # noqa: F401

        return True
    except ImportError:
        return False


def _load():
    global _model
    if _model is None:
        import hanlp

        _model = hanlp.load(
            hanlp.pretrained.mtl.CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_SMALL_ZH,
            devices=-1,
        )
    return _model


class HanlpNER:
    """Batch-oriented NER; core primes the findings cache with find_batch."""

    def find_batch(self, parts: list[str]) -> dict[str, list[Finding]]:
        """Best-effort: if the model can't load (not downloaded, no network,
        torch mismatch), degrade to 'no findings' instead of killing the scan
        -- jieba and the heuristics still cover the common cases."""
        global _broken
        if _broken:
            return {}
        try:
            model = _load()
        except Exception as exc:
            _broken = True
            print(f"[privacyfs] HanLP model unavailable ({type(exc).__name__}), "
                  "continuing with jieba/heuristics", file=sys.stderr)
            return {}
        todo = [p for p in parts if _CJK.search(p)]
        out: dict[str, list[Finding]] = {}
        if not todo:
            return out
        try:
            for i in range(0, len(todo), 32):
                chunk = todo[i : i + 32]
                # batch input -> ONE dict: task -> list of per-sentence entity lists
                results = model(chunk, tasks="ner*")
                for key, per_sentence in results.items():
                    if "ner" not in key or not isinstance(per_sentence, list):
                        continue
                    for text, entities in zip(chunk, per_sentence):
                        for surface, tag, _s, _e in entities or []:
                            category = _CATEGORY.get(tag.upper())
                            surface = _STRIP.sub("", surface)
                            if not category or len(surface) < 2:
                                continue
                            findings = out.setdefault(text, [])
                            if not any(f.surface == surface for f in findings):
                                findings.append(Finding(category, surface))
        except Exception as exc:
            _broken = True
            print(f"[privacyfs] HanLP inference failed ({type(exc).__name__}), "
                  "partial results kept", file=sys.stderr)
        return out

    # single-name fallback for interface compatibility
    def find(self, name: str) -> list[Finding]:
        return self.find_batch([name]).get(name, [])
