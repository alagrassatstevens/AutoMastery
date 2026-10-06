import io

import pandas as pd


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


def download_grades_df(course, assignment_name):
    """Grades for an assignment, merging every version if it is a versioned test."""
    assignments = course.gradescope.get_assignments(course.gs_course)
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


class DuplicateSubmissionError(RuntimeError):
    def __init__(self, sids):
        super().__init__(
            "Multiple non-missing submissions across versions for SID(s): "
            f"{', '.join(str(s) for s in sids)}. "
            "Each student should submit one version; check Gradescope.")
