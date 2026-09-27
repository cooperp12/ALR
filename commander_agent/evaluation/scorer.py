import difflib
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from commander_agent.config import SCORE_FILE
def find_tests_file():
    cwd = Path.cwd()
    here = Path(__file__).resolve()

    candidates = [
        cwd / "benchmarks" / "botsv3_q1_q3_q6_benchmark.json",
        cwd / "benchmarks" / "botsv3_q1_q3_benchmark.json",
        cwd / "botsv3_b2_tests.json",
        cwd.parent / "lab2" / "botsv3_b2_tests.json",
    ]

    # Modular installs may sit under labs\lab3\ALR_modular. Walk upward and
    # check both the current ancestor and a sibling lab2 directory.
    for parent in [cwd, *cwd.parents, here.parent, *here.parents]:
        candidates.append(parent / "benchmarks" / "botsv3_q1_q3_q6_benchmark.json")
        candidates.append(parent / "benchmarks" / "botsv3_q1_q3_benchmark.json")
        candidates.append(parent / "botsv3_b2_tests.json")
        candidates.append(parent / "lab2" / "botsv3_b2_tests.json")

    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            return path
    return None

def load_lab_tests():
    path = find_tests_file()

    if not path:
        return None, None

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if isinstance(data, dict):
            return data, path

    except Exception as exc:
        print(f"[Scorer warning: {type(exc).__name__}: {exc}]")

    return None, path

def load_score_oracle(tests_path):
    if not tests_path:
        return {}
    candidates = [
        Path(tests_path).parent / "score_oracle.sha256.json",
        Path.cwd() / "benchmarks" / "score_oracle.sha256.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def normalise_answer_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def answer_digest(value):
    return hashlib.sha256(normalise_answer_text(value).encode("utf-8")).hexdigest()


def normalise_question_text(value):
    value = (value or "").lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def match_lab_test(question, tests):
    if not tests:
        return None, None, 0.0

    target = normalise_question_text(question)

    best_qid = None
    best_test = None
    best_ratio = 0.0

    for qid, test in tests.items():
        if not isinstance(test, dict):
            continue

        candidate = test.get("question") or ""
        candidate_norm = normalise_question_text(candidate)

        if not candidate_norm:
            continue

        if target == candidate_norm:
            return qid, test, 1.0

        ratio = difflib.SequenceMatcher(
            None,
            target,
            candidate_norm,
        ).ratio()

        if ratio > best_ratio:
            best_qid = qid
            best_test = test
            best_ratio = ratio

    if best_ratio >= 0.72:
        return best_qid, best_test, best_ratio

    return None, None, best_ratio

def answer_matches_patterns(answer, patterns):
    answer = answer or ""

    for pattern in patterns or []:
        if pattern.startswith("re:"):
            if re.search(pattern[3:], answer, re.IGNORECASE):
                return True
        elif pattern.lower() in answer.lower():
            return True

    return False

def update_score_file(qid, test, answer, passed, metrics, skills):
    try:
        existing = {}
        score_path = Path(SCORE_FILE)

        if score_path.exists():
            try:
                existing = json.loads(score_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}

        existing[qid] = {
            "name": test.get("name"),
            "difficulty": test.get("difficulty"),
            "passed": bool(passed),
            "answer": answer or "",
            "skills": list(skills),
            "rounds": metrics.get("rounds_used"),
            "tool_calls": metrics.get("tool_calls"),
            "search_calls": metrics.get("search_calls"),
            "search_attempts": metrics.get("search_attempts"),
            "query_cache_hits": metrics.get("query_cache_hits"),
            "validation_reviews": metrics.get("validation_reviews"),
            "candidate_answers": metrics.get("candidate_answers"),
            "final_validation_status": metrics.get("final_validation_status"),
            "updated": datetime.now().isoformat(),
        }

        score_path.write_text(
            json.dumps(existing, indent=2),
            encoding="utf-8",
        )

    except Exception as exc:
        print(f"[Could not update score file: {exc}]")

def score_final_answer(question, answer, metrics, skills):
    tests, tests_path = load_lab_tests()

    print()
    print("=" * 70)
    print("LAB SCORER")
    print("=" * 70)

    if not tests:
        print("Scoring unavailable: botsv3_b2_tests.json was not found.")
        return None

    qid, test, ratio = match_lab_test(question, tests)

    if not test:
        print(
            "The question did not match a canonical BOTSv3 scoring test closely "
            f"enough (best match {ratio:.2f})."
        )
        print("Answer left unscored.")
        return None

    oracle = load_score_oracle(tests_path)
    expected_digest = str(oracle.get(qid) or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        print("Scoring unavailable: no isolated hash oracle entry for this test.")
        return None
    passed = answer_digest(answer) == expected_digest

    print(f"Matched test : {qid}")
    print(f"Name         : {test.get('name', '')}")
    print(f"Difficulty   : {test.get('difficulty', '')}")
    print(f"Result       : {'PASS' if passed else 'CHECK'}")
    if not passed:
        print("Expected value is intentionally not stored or displayed in plaintext.")

    update_score_file(
        qid,
        test,
        answer,
        passed,
        metrics,
        skills,
    )

    try:
        saved = json.loads(Path(SCORE_FILE).read_text(encoding="utf-8"))
        correct = sum(1 for item in saved.values() if item.get("passed"))
        print(f"Cumulative   : {correct}/{len(saved)} attempted correct")
        print(f"Score file   : {SCORE_FILE}")
        print(f"Test source  : {tests_path}")
    except Exception:
        pass

    return bool(passed)

