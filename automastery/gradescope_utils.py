import io
import logging
import re

import pandas as pd

logger = logging.getLogger(__name__)


def gradescope_title(assignment_name):
    """Canvas names exam questions 'Test 1 Question 2'; Gradescope groups them under 'Test 1'."""
    if "Question" in assignment_name:
        return assignment_name[:assignment_name.index("Question") - 1]
    return assignment_name


def find_assignment_attempts(assignments, assignment_name):
    """Top-level assignments whose title matches the Canvas title. One is the normal case;
    more than one (e.g. initial and retake) triggers the per-subquestion max."""
    title = gradescope_title(assignment_name)
    attempts = [a for a in assignments if not a.container_id and a.title.startswith(title)
                and not a.title[len(title):len(title) + 1].isalnum()]
    if not attempts:
        raise ValueError(f"Could not find assignment name: {assignment_name}")
    return attempts


def download_grades(course, assignment):
    response = course.gradescope.session.get(assignment.get_grades_url())
    course.gradescope._response_check(response)
    return pd.read_csv(io.StringIO(response.content.decode("utf-8")))


def clean_grades(df):
    df = df[df["First Name"] != "unidentified"].reset_index(drop=True)
    df["SID"] = df["SID"].astype("Int64")
    return df


def question_columns(df):
    return [c for c in df.columns if "pts)" in c and "ptional" not in c]


def check_version_keys(version_to_cols):
    names = list(version_to_cols)
    all_cols = set().union(*version_to_cols.values())
    mismatches = {
        col: [n for n in names if col in version_to_cols[n]]
        for col in all_cols
        if sum(col in version_to_cols[n] for n in names) != len(names)
    }
    if mismatches:
        raise VersionKeyMismatchError(mismatches, names)


def combine_versions(dfs):
    combined = clean_grades(pd.concat(dfs, axis=0, join="inner", ignore_index=True))

    with_sid = combined[combined["SID"].notna()]
    without_sid = combined[combined["SID"].isna()]

    kept = []
    conflicts = []
    for sid, group in with_sid.groupby("SID"):
        # A student submits one version; in the others they appear as Missing.
        non_missing = group[group["Status"] != "Missing"]
        if len(non_missing) > 1:
            conflicts.append(int(sid))
        else:
            kept.append(non_missing if len(non_missing) == 1 else group.iloc[[0]])
    if conflicts:
        raise DuplicateSubmissionError(conflicts)

    return pd.concat(kept + [without_sid], ignore_index=True)


def download_and_combine_versions(course, versions):
    version_to_df = {a.version_name: download_grades(course, a) for a in versions}
    check_version_keys({n: set(question_columns(df)) for n, df in version_to_df.items()})
    return combine_versions(list(version_to_df.values()))


def question_key(col):
    """Key that identifies a subquestion across attempts even if the header text differs."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*:\s*([a-zA-Z]+)?", col)
    if m:
        return f"{m.group(1)}:{(m.group(2) or '').lower()}"
    return re.sub(r"\s*\(\d+(?:\.\d+)?\s*pts\)\s*$", "", col).strip()


def download_attempt(course, assignments, attempt):
    """Download attempt directly combines versions for scores
    due to a change where Gradescope only seems to contain container's scores.csv
    but not each versions """
    versions = sorted((a for a in assignments if a.container_id == attempt.assignment_id),
                      key=lambda a: a.version_index)
    if versions:
        return clean_grades(download_and_combine_versions(course, versions))
    return clean_grades(download_grades(course, attempt))


def max_across_attempts(dfs):
    """Per-subquestion max across sittings; dfs[0] is the base and supplies the output headers.
    A student missing from a sitting scores 0 there; missing from all stays Missing."""
    maps = [{question_key(c): c for c in question_columns(df)} for df in dfs]
    check_version_keys({f"sitting {i}": set(m) for i, m in enumerate(maps)})

    indexed = [df[df["SID"].notna()].set_index("SID") for df in dfs]
    all_sids = indexed[0].index
    for df in indexed[1:]:
        all_sids = all_sids.union(df.index)
    indexed = [df.reindex(all_sids) for df in indexed]

    merged = indexed[0].copy()
    for key, ref_col in maps[0].items():
        per_sitting = [pd.to_numeric(df[m[key]], errors="coerce").fillna(0)
                       for df, m in zip(indexed, maps)]
        merged[ref_col] = pd.concat(per_sitting, axis=1).max(axis=1)

    present = pd.concat([df["Status"].notna() & df["Status"].ne("Missing") for df in indexed],
                        axis=1).any(axis=1)
    merged["Status"] = present.map({True: "Graded", False: "Missing"})
    return merged.reset_index()


def download_grades_df(course, assignment_name):
    """Grades for an assignment, merging every version, and taking the per-subquestion max
    across multiple attempts (e.g. initial and retake) when more than one exists."""
    assignments = course.gradescope.get_assignments(course.gs_course)
    attempts = find_assignment_attempts(assignments, assignment_name)
    if len(attempts) == 1:
        return download_attempt(course, assignments, attempts[0])
    base = [s for s in attempts if "retake" not in s.title.lower()]
    if len(base) != 1:
        raise AmbiguousReferenceSittingError([s.title for s in attempts])
    ordered = base + [s for s in attempts if s not in base]
    logger.info("Taking the per-subquestion max across attempts for %s: %s",
                assignment_name, [s.title for s in ordered])
    return max_across_attempts([download_attempt(course, assignments, s) for s in ordered])


class VersionKeyMismatchError(RuntimeError):
    def __init__(self, mismatches, all_versions):
        lines = ["Subquestion columns differ across test versions. "
                 "Fix on Gradescope so every version uses identical headers:"]
        for col, present in sorted(mismatches.items()):
            missing = [v for v in all_versions if v not in present]
            lines.append(f"  {col!r}")
            lines.append(f"      present in: {present}")
            lines.append(f"      missing in: {missing}")
        super().__init__("\n".join(lines))


class AmbiguousReferenceSittingError(RuntimeError):
    def __init__(self, titles):
        super().__init__(
            "Expected exactly one non-retake attempt to anchor the column headers, but matched "
            f"these top-level assignments: {titles}. "
            "Rename on Gradescope so only the base attempt lacks 'retake'.")


class DuplicateSubmissionError(RuntimeError):
    def __init__(self, sids):
        super().__init__(
            "Multiple non-missing submissions across versions for SID(s): "
            f"{', '.join(str(s) for s in sids)}. "
            "Each student should submit one version; check Gradescope.")
