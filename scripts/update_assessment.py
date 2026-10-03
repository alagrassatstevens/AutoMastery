import argparse
import logging

from automastery.assignment import make_assignment_from_name
from automastery.course import Course


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--course_id", help="Course ID", default=80807)
    parser.add_argument( "-a", "--assignment_name", help="assignment to update")
    parser.add_argument("-s", "--student_name_match",  help="")
    args = parser.parse_args()


    course = Course("https://sit.instructure.com/api/v1", args.course_id, overwrite_assignment_json=True)
    assignment_id = course.find_assignment_id_by_name(args.assignment_name)
    assignment = make_assignment_from_name(args.assignment_name, assignment_id, course)
    assignment.update_mastery_scores(student_name_match=args.student_name_match)


if __name__ == "__main__":
    main()