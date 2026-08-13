"""
kb/eval.py — standing Q/A regression set for the Ask feature.

Each case in eval_qa_set.json pins a question to a specific fact that's already
in the library (see the "source" field on each case). A case "passes" when every
one of its expected_keywords shows up, case-insensitively, somewhere in Ask's
generated answer text. This is a coarse but dependency-free check — cheap, and
it catches regressions where a fact silently drops out of extraction, entity
linking, or retrieval, without needing a second LLM call to judge phrasing.

Run from the Admin page, or directly:
    python -m kb.eval
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import ROOT_DIR
from kb.retrieval import retrieve_and_answer

log = logging.getLogger("sn1.eval")

EVAL_SET_PATH = ROOT_DIR / "eval_qa_set.json"


@dataclass
class EvalResult:
    id: str
    question: str
    expected_answer: str
    expected_keywords: list
    source: str
    actual_answer: str
    passed: bool
    missing_keywords: list = field(default_factory=list)
    error: str = ""


def load_eval_set(path: Path = EVAL_SET_PATH) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def run_case(case: dict) -> EvalResult:
    """Run one eval case against the live Ask pipeline. Never raises — an
    exception is captured as a failed case with `error` set, so one broken
    case can't abort the rest of the run."""
    try:
        result = retrieve_and_answer(case["question"])
        answer = result.get("answer", "") or ""
    except Exception as e:
        log.warning("Eval case %s raised: %s", case.get("id"), e)
        return EvalResult(
            id=case.get("id", ""), question=case["question"],
            expected_answer=case.get("expected_answer", ""),
            expected_keywords=case.get("expected_keywords", []),
            source=case.get("source", ""),
            actual_answer="", passed=False,
            missing_keywords=case.get("expected_keywords", []),
            error=str(e),
        )

    answer_lower = answer.lower()
    missing = [kw for kw in case.get("expected_keywords", []) if kw.lower() not in answer_lower]
    return EvalResult(
        id=case.get("id", ""), question=case["question"],
        expected_answer=case.get("expected_answer", ""),
        expected_keywords=case.get("expected_keywords", []),
        source=case.get("source", ""),
        actual_answer=answer,
        passed=not missing,
        missing_keywords=missing,
    )


def run_eval_set(path: Path = EVAL_SET_PATH) -> list[EvalResult]:
    """Run every case sequentially (Ask itself calls Claude, so we don't
    parallelise — keeps this friendly to the CLI-subprocess LLM fallback)."""
    return [run_case(c) for c in load_eval_set(path)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = run_eval_set()
    n_pass = sum(1 for r in results if r.passed)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"{status}  {r.id}: {r.question}")
        if not r.passed:
            print(f"      expected keywords missing: {r.missing_keywords}")
            print(f"      actual answer: {r.actual_answer[:200]}")
            if r.error:
                print(f"      error: {r.error}")
    print(f"\n{n_pass}/{len(results)} passed")
