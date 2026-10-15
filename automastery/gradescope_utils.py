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


def find_assignment_by_name(assignments, assignment_name):
    title = gradescope_title(assignment_name)
    for assignment in assignments:
        if title in assignment.title:
            return assignment
    raise ValueError(f"Could not find assignment name: {assignment_name}")


def find_version_assignments(assignments, assignment_name):
    title = gradescope_title(assignment_name)
    container = next((a for a in assignments if a.title == title), None)
    if container is None:
        return None
    versions = [a for a in assignments if a.container_id == container.assignment_id]
    if not versions:
        return None
    return sorted(versions, key=lambda a: a.version_index)


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
    """Key that identifies a subquestion across sittings even if the header text differs."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*:\s*([a-zA-Z]+)?", col)
    if m:
        return f"{m.group(1)}:{(m.group(2) or '').lower()}"
    return re.sub(r"\s*\(\d+(?:\.\d+)?\s*pts\)\s*$", "", col).strip()


def find_max_sittings(assignments, assignment_name):
    """Top-level assignments whose title matches the Canvas title (e.g. initial and retake),
    ordered with the non-retake base first. None means a single sitting: use the version path."""
    title = gradescope_title(assignment_name)
    sittings = [a for a in assignments if not a.container_id and (
        a.title == title or
        (a.title.startswith(title) and not a.title[len(title):len(title) + 1].isalnum()))]
    if len(sittings) <= 1:
        return None
    base = [s for s in sittings if "retake" not in s.title.lower()]
    if len(base) != 1:
        raise AmbiguousReferenceSittingError([s.title for s in sittings])
    return base + [s for s in sittings if s not in base]


def download_sitting(course, assignments, sitting):
    versions = find_version_assignments(assignments, sitting.title)
    if versions:
        return download_and_combine_versions(course, versions)
    return clean_grades(download_grades(course, sitting))


def max_across_sittings(dfs):
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
    across multiple sittings (e.g. initial and retake) when more than one exists."""
    assignments = course.gradescope.get_assignments(course.gs_course)
    sittings = find_max_sittings(assignments, assignment_name)
    if sittings:
        logger.info("Taking the per-subquestion max across sittings for %s: %s",
                    assignment_name, [s.title for s in sittings])
        return max_across_sittings([download_sitting(course, assignments, s) for s in sittings])
    versions = find_version_assignments(assignments, assignment_name)
    if versions:
        return download_and_combine_versions(course, versions)
    return clean_grades(download_grades(course, find_assignment_by_name(assignments, assignment_name)))


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
            "Expected exactly one non-retake sitting to anchor the column headers, but matched "
            f"these top-level assignments: {titles}. "
            "Rename on Gradescope so only the base sitting lacks 'retake'.")


class DuplicateSubmissionError(RuntimeError):
    def __init__(self, sids):
        super().__init__(
            "Multiple non-missing submissions across versions for SID(s): "
            f"{', '.join(str(s) for s in sids)}. "
            "Each student should submit one version; check Gradescope.")
