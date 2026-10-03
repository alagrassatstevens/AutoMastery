import argparse
import logging
import os

from automastery.assignment import Assignment, make_assignment_from_name
from automastery.course import Course


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--course_id", help="Course ID", default=84995)
    parser.add_argument("-s", "--student_name_match",  help="")
    parser.add_argument("-f", "--first_assessment", help="which assessment to start with", default=None)
    args = parser.parse_args()


    course = Course("https://sit.instructure.com/api/v1", args.course_id, overwrite_assignment_json=True)
    assignment_order_fn = course.course_config_root / "assignment_order.txt"
    if not os.path.exists(assignment_order_fn):
        print(f"Could not find assignment order file {assignment_order_fn}. Please make a .txt file with the names"
              f"of assignments in the order of recency")
    with open(assignment_order_fn) as f:
        assignments_by_order = f.read().splitlines()
    found_first_assessment = False
    for assignment_name in assignments_by_order:
        if args.first_assessment:
            found_first_assessment = found_first_assessment or assignment_name == args.first_assessment
            if not found_first_assessment:
                continue
        assignment_id = course.find_assignment_id_by_name(assignment_name)
        assignment = make_assignment_from_name(assignment_name, assignment_id, course)
        print(f"Updating assignment {assignment_name}")

        assignment.update_mastery_scores(student_name_match=args.student_name_match)
        print(f"Updated mastery scores for assignment {assignment_name}")


if __name__ == "__main__":
    main()
