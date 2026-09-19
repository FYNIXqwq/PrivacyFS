"""Product adapters are separate from attackers and gold scorers."""
from collections import Counter
from dataclasses import dataclass, replace
import csv
import io
import hashlib

from privacyfs.config import Rules
from privacyfs.documents import DocumentSession, inspect_document, ParseStatus
from privacyfs.evidence import EvidenceGraph
from privacyfs.planning import build_plan, PLANNER_VERSION
from privacyfs.relations import FieldSpec, RecordTemplate
from privacyfs.task_profiles import TaskProfile, FieldPolicy
from privacyfs.verifier import verify_candidate, ReleaseRejected

from .attacks import Table


@dataclass(frozen=True)
class DetectionInput:
    documents: tuple[str, ...]
    names: tuple[str, ...]
    organizations: tuple[str, ...]


class RuleDetector:
    version = "d1-rules-v1/isolated-eval-policy-v1"

    def predict(self, data, root):
        # Use explicit lists with no inherited default keyword/profession noise.
        rules = Rules(literal_names=list(data.names), literal_orgs=list(data.organizations),
                      keywords=[], regexes=[], detect_identity=False, detect_professions=False)
        results = set()
        with DocumentSession(root) as session:
            for index, text in enumerate(data.documents):
                path = root / f"document-{index}.txt"
                path.write_bytes(text.encode("utf-8"))
                parsed = session.parse(session.capture(path.name))
                if parsed.coverage.status is not ParseStatus.COMPLETE:
                    raise ValueError("incomplete detection input")
                for finding in inspect_document(parsed, rules):
                    category = {"NAME_LIST": "PERSON"}.get(finding.category, finding.category)
                    results.add((index, category, finding.locator.start, finding.locator.end))
        return results


class EmptyDetector:
    version = "abstain-v1"

    def predict(self, data, root):
        return set()


DETECTORS = {"rules": RuleDetector(), "abstain": EmptyDetector()}
TREATMENT_VERSIONS = {"raw": "raw-v1", "identity_only": "drop-name-retain-keys-v1",
                      "privacyfs": PLANNER_VERSION, "suppress_all": "empty-view-v1"}


def csv_bytes(table):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(table.columns)
    writer.writerows(table.rows)
    return stream.getvalue().encode("utf-8")


def read_table(kind, payload):
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8"), newline="")))
    if not rows or len(set(rows[0])) != len(rows[0]) or any(len(r) != len(rows[0]) for r in rows[1:]):
        raise ValueError("invalid artifact table")
    return Table(kind, tuple(rows[0]), tuple(tuple(r) for r in rows[1:]))


def make_release_input(source):
    return tuple(Table(t["kind"], tuple(t["columns"]), tuple(tuple(r) for r in t["rows"])) for t in source)


def transform(tables, treatment, root):
    """Returns read-back public tables and private positional subject alignment.

    Alignment is used ONLY for scoring. It is never attached to AttackView.
    """
    originals = {t.kind: t for t in tables}
    clients = originals["clients"].records()
    if treatment == "suppress_all":
        return (), {r["cid"]: f"unreleased-{i}" for i, r in enumerate(clients)}
    if treatment != "privacyfs":
        output = tables
        if treatment == "identity_only":
            output = tuple(Table(t.kind, t.columns, tuple(
                tuple("" if col == "name" else value for col, value in zip(t.columns, row))
                for row in t.rows)) for t in tables)
        if treatment not in {"raw", "identity_only"}:
            raise ValueError("unknown treatment")
        return output, {r["cid"]: r["cid"] for r in clients}

    ck = FieldSpec("cid", "key", "eval-clients", "client")
    mk = FieldSpec("mid", "key", "eval-meetings", "event")
    facts = originals["facts"].records()
    values = tuple(sorted({r["fact"] for r in facts}))
    topics = tuple(sorted({r["topic"] for r in facts}))
    templates = {"clients": RecordTemplate((ck, FieldSpec("name", "identity")), "cid"),
                 "bridges": RecordTemplate((ck, mk), "cid"),
                 "facts": RecordTemplate((mk, FieldSpec("topic", "label"),
                                          FieldSpec("fact", "sensitive", values=values)),
                                         "mid", "assertion_state")}
    # The task deliberately requires the topic as context; external knowledge is
    # absent from product policy. The resulting residual attack is NOT hidden.
    profile = TaskProfile("collaboration_statistics", "synthetic-eval-recipient", (
        FieldPolicy("cid", "cid", "alias"), FieldPolicy("mid", "mid", "alias"),
        FieldPolicy("topic", "topic", approved_values=topics),
        FieldPolicy("fact", "fact", approved_values=values)))
    original_bytes = {}
    with DocumentSession(root) as session:
        bindings = []
        for index, table in enumerate(tables):
            name = f"input-{index}.csv"
            payload = csv_bytes(table)
            (root / name).write_bytes(payload)
            original_bytes[name] = payload
            bindings.append((session.parse(session.capture(name)), templates[table.kind]))
        with EvidenceGraph(bindings) as graph:
            # Reproducible synthetic benchmark ONLY. Real releases keep random seeds.
            seed = hashlib.sha256(b"effectiveness-v1" + b"".join(original_bytes.values())).digest()
            try:
                plan = build_plan(bindings, graph, graph.analyze(), profile, seed=seed)
            except ReleaseRejected:
                return None, {}
            if plan.selected is None:
                return None, {}
            candidate = plan.candidates[plan.selected]
            output = root / "output"
            output.mkdir()
            artifacts, public = [], []
            for artifact in candidate.artifacts:
                target = output / artifact.filename
                target.write_bytes(artifact.payload)
                readback = replace(artifact, payload=target.read_bytes())
                artifacts.append(readback)
                public.append(read_table(tables[artifact.document_index].kind, readback.payload))
            verify_candidate(plan, replace(candidate, artifacts=tuple(artifacts)))
            if any((root / name).read_bytes() != value for name, value in original_bytes.items()):
                raise ValueError("source changed")
    released_clients = next(t for t in public if t.kind == "clients").records()
    if len(released_clients) != len(clients):
        raise ValueError("unsupported scoring alignment")
    alignment = {a["cid"]: b["cid"] for a, b in zip(clients, released_clients)}
    return tuple(public), alignment


def task_score(tables, gold_rows, expected_relations, alignment):
    facts = [r for t in tables if t.kind == "facts" for r in t.records()]
    bridges = [r for t in tables if t.kind == "bridges" for r in t.records()]
    clients = {r["cid"] for t in tables if t.kind == "clients" for r in t.records()}
    observed = Counter((r["topic"], r["fact"], r["assertion_state"]) for r in facts)
    expected = Counter(tuple(r) for r in gold_rows)
    # Scorer retains row multiplicity and checks the privately aligned owner.
    links = Counter((b["cid"], f["topic"], f["fact"], f["assertion_state"])
                    for b in bridges for f in facts if b["mid"] == f["mid"] and b["cid"] in clients)
    desired = Counter((alignment[s], topic, fact, state) for s, topic, fact, state in expected_relations)
    return {"exact": observed == expected and links == desired,
            "facts": score_counter(expected, observed), "relations": score_counter(desired, links)}


def score_counter(gold, observed):
    from .metrics import counts_score
    tp = sum((gold & observed).values())
    return counts_score(tp, sum(observed.values()) - tp, sum(gold.values()) - tp)
