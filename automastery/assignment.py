import copy
import io
import json
import os

import tqdm
from abc import ABC, abstractmethod
from typing import Union, Any

import numpy as np
import pandas as pd
import re
from pathlib import Path

from .utils import assignment_match_to_csv_name, StudentNotFoundError, StudentSubmissionNotFoundError, \
    find_student_df_by_SID, RubricNotFoundError, find_csv_in_dir
from .course import Course
from gradescope import save_csv

import requests


''' Class to hold data for an assignment, including questions and subquestions
    Note: Canvas assignments are one assignment per lab/HW/whatever, but PER QUESTION for each exam question.
'''

class Assignment(ABC):
    def __init__(self, name: str, assignment_id: int, course: Course, update_from_gradescope:bool=True):
        """

        Args:
            name: The name of the assignment, Homework 4, Exam 1, etc..
                must match what the name is on Canvas
            assignment_id: The Canvas assignment ID
            course: Course object.
        """
        self.name = name
        self.course = course
        self.assignment_id = assignment_id
        self.update_from_gradescope = update_from_gradescope
        self.assignment_config_path = self.course.course_config_root / f"assignment_{assignment_id}"

        if not os.path.exists(self.assignment_config_path): # Make directory for assignment if doesn't exist already.
            os.mkdir(self.assignment_config_path)

        if os.path.exists(self.assignment_config_path / "score_thresholds.json"):
            with open(self.assignment_config_path / "score_thresholds.json") as f:
                self.score_thresholds = json.load(f)
        else:
            self.score_thresholds = {}
            threshold_defaults = {"Exceeds Mastery":0.99, "Mastery":0.75, "Near Mastery":0.5, "Below Mastery":0.25}
            for threshold in threshold_defaults.keys():
                user_threshold = input(f"Enter threshold for {threshold} or <Enter> for default: ")
                if user_threshold == "":
                   threshold_value = threshold_defaults[threshold]
                else:
                    threshold_value = float(user_threshold)

                self.score_thresholds[threshold] = threshold_value
            with open(self.assignment_config_path / "score_thresholds.json", "w") as f:
                json.dump(self.score_thresholds, f)


    @property
    @abstractmethod
    def need_to_update_total_question_score(self)->bool:
        """
        Returns: Whether a total_question_score needs to be updated.
        """
        raise NotImplementedError()

    @abstractmethod
    def compute_new_outcome(self, sid :int, student_name: str, submission_url:str)->dict:
        """

        Args:
            sid: Canvas Student ID
            student_name: student name (mostly for logging purposes)
            submission_url: correctly formatted Canvas API submission URL

        Returns:
            A dictionary corresponding to the new outcome, which is compatible with the
            PUT/POST request to update rubric items for a submission.
        """
        pass

    def score_to_rubric_score(self, score:float)->int:
        """

        Args:
            score: A number from 0 to 1 corresponding to the percentage

        Returns:
            A mastery rubric score from 1 to 4.

        """
        if score >= self.score_thresholds["Exceeds Mastery"]:
            return 4
        elif score >= self.score_thresholds["Mastery"]:
            return 3
        elif score >= self.score_thresholds["Near Mastery"]:
            return 2
        elif score >= self.score_thresholds["Below Mastery"]:
            return 1
        else:
            return 0

    def update_mastery_scores(self, student_name_match :str = None):
        """

        Updates mastery scores on Canvas for this assignment for all students, or for
        a student who matches a particular name, student_name_match

        Args:
            student_name_match: Optional argument of a student's name to update,
            if not updating scores for all students.

        """
        student_data_dict = self.course.student_data_dict #read only
        for sid in tqdm.tqdm(student_data_dict.keys()):
            if student_name_match is not None:
                if student_name_match not in student_data_dict[sid]["name"]:
                    continue
            self.update_mastery_score_for_student(int(sid), student_data_dict[sid])

    def update_mastery_score_for_student(self, sid:int, student_data_dict:dict):
        """
        Updates mastery score for a particular student on Canvas
        Args:
            sid: Canvas student ID
            student_data_dict: A dictionary containing all the student's Canvas data
               with their Canvas student ID as keys.

        """
        student_id = student_data_dict["id"]
        submission_url = f"{self.course.PAGE_URL}/courses/{self.course.COURSE_ID}/assignments/{self.assignment_id}/submissions/{student_id}"
        student_name = student_data_dict["short_name"]
        try:
            new_outcome = self.compute_new_outcome(sid, student_name, submission_url)
        except StudentSubmissionNotFoundError as e:
            print(e)
            return
        except StudentNotFoundError as e:
            print(e)
            return

        # Prevents posting the comment again
        if "comment" in new_outcome:
            if self._comment_already_posted(submission_url, new_outcome["comment"]["text_comment"]):
                print("Comment already posted")
                del new_outcome["comment"]["text_comment"]
            else:
                print("Adding comment", new_outcome["comment"]["text_comment"])

        # Needs to be done without the score to work for some reason
        out_response = requests.put(submission_url, headers=self.course.headers, json=new_outcome)
        try:
            out_response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(e)
            print(f"Unable to update for {student_name}")
            return

        if self.need_to_update_total_question_score:
            # Need to keep both of these temporarily until we wipe all the Canvas points
            met_all = np.all([res["points"] > 0 for res in new_outcome["rubric_assessment"].values()])
            submission_data =  {"submission[posted_grade]": float(0)}
            # Old: for points. Keeping for convenience
            #total_question_score = self.compute_total_question_score(sid, student_name)
            # submission_data =  {"submission[posted_grade]": float(total_question_score)}
            out_response = requests.put(submission_url, headers=self.course.headers, json=new_outcome,
                                        data=submission_data)
            out_response.raise_for_status()
            submission_data =  {"submission[posted_grade]": "complete" if met_all else "incomplete"}
            out_response = requests.put(submission_url, headers=self.course.headers, json=new_outcome,
                                        data=submission_data)
            out_response.raise_for_status()

    def _comment_already_posted(self, submission_url:str, text:str)->bool:
        response = requests.get(submission_url, headers=self.course.headers,
                                params={"include[]": "submission_comments"})
        response.raise_for_status()
        existing = response.json().get("submission_comments", [])
        return any(c["comment"] == text for c in existing)


    def compute_total_question_score(self, sid:int, student_name:str)->int:
        """

        Only needs to be implemented if the class has self.need_to_update_total_question_score

        Args:
            sid: Student ID on Canvas
            student_name: student name on canvas (for logging purposes)

        Returns:
            Total question score (as in for the entire question, not subparts)
            for this assignment.

        """
        pass


class LoadFromCSVAssignment(Assignment):
    """
    An assignment that's loaded from a Gradescope CSV
    The name maps to a CSV file (not the cleanest I know...)
    """
    def __init__(self, name:str, assignment_id:int, course:Course, update_from_gradescope:bool=True):
        super().__init__(name, assignment_id, course, update_from_gradescope)

        # Set up data directories
        self.assignment_data_path = self.course.course_data_root / f"assignment_{assignment_id}"
        if not os.path.exists(self.assignment_data_path):
            os.makedirs(str(self.assignment_data_path))
            print("Created assignment data directory")

        csv_file_name = self.get_csv_file_name()
        if self.update_from_gradescope:
            self.update_csv_from_gradescope(csv_file_name)

        self.score_df = pd.read_csv(csv_file_name)
        self.rubric_id_to_qkeys = self.load_rubric_id_to_qkeys() #load from a json file
        self.rubric_id_to_total_pts = self.get_rubric_id_to_total_pts(self.rubric_id_to_qkeys)

    def update_csv_from_gradescope(self, csv_file_name):
        """
        Mutator: updates the csv file
        """
        # Get assignment
        assignments = self.course.gradescope.get_assignments(self.course.gs_course)
        gradescope_assignment = self.get_gradescope_assignment_by_name(assignments, self.name)

        # Save the df to data/
        # grade_df = self.course.gradescope.get_assignment_grades(gradescope_assignment)
        response = self.course.gradescope.session.get(gradescope_assignment.get_grades_url())
        self.course.gradescope._response_check(response)
        grade_df = pd.read_csv(io.StringIO(response.content.decode('utf-8')))
        grade_df = grade_df[grade_df["First Name"] != "unidentified"].reset_index(drop=True)
        grade_df["SID"] = grade_df["SID"].astype("Int64")
        save_csv(csv_file_name, grade_df)

    def get_csv_file_name(self) -> Any:
        #! Create assignments JSON if does not exist already.
        if not os.path.exists(self.assignment_config_path / "assignment.json"):
            with open(self.assignment_config_path / "assignment.json", 'x') as temp_f:
                print("Created file:", str(self.assignment_config_path / "assignment.json"))
                json.dump({}, temp_f)


        # Assumes this file has been created already
        with open(self.assignment_config_path / "assignment.json", 'r', encoding='utf-8') as file:
            data_dict = json.load(file)
            if "csv_path" not in data_dict:
                user_resp = input("Download CSV from Gradescope? y/n :")
                if "y" in user_resp.lower():
                    assert(self.update_from_gradescope)
                    # Make the name that would be on gradescope
                    assignment_name = copy.deepcopy(self.name)
                    if "Test" in assignment_name:
                        assignment_name = assignment_name[:assignment_name.index("Question") - 1]
                    assignment_name = assignment_name.replace(" ", "_")
                    csv_file_path = str(self.assignment_data_path / f"{assignment_name}_scores.csv")
                else:
                    potential_csv_file_name = input(f"Enter a CSV file name, or type press and put the file in {self.assignment_data_path}. Enter when done: ")
                    #? Using my own deduction from the code
                    if potential_csv_file_name.endswith(".csv"):
                        csv_file_name = potential_csv_file_name
                        # File path of the csv
                        csv_file_path = self.assignment_data_path / csv_file_name
                    else:
                        csv_file_name = find_csv_in_dir(self.assignment_data_path)
                    print(f"Using csv {csv_file_name}")

                data_dict["csv_path"] = csv_file_path
            with open(self.assignment_config_path / "assignment.json", 'w') as fp:
                json.dump(data_dict, fp)
            csv_file_path = data_dict["csv_path"]
        return csv_file_path


    def get_gradescope_assignment_by_name(self, assignments, assignment_name):
        if "Test" in assignment_name:
            # If we're doing Test X Question Y
            assignment_name = assignment_name[:assignment_name.index("Question")-1]
        for assignment in assignments:
            if assignment_name in assignment.title:
                return assignment
        raise ValueError(f"Could not find assignment name:  {assignment_name}")
    ###! End of Jamil Shenanigans

    @property
    def need_to_update_total_question_score(self)->bool:
        return True

    def load_rubric_id_to_qkeys(self)->dict:
        """
            Checks if there is a file for that assignment id
            corresponding to the question keys
            and makes one if not.
        """
        #looks for the filename
        filename = self.assignment_config_path / f"rubric_id_to_question_keys.json"
        if not filename.exists():
            rubric_id_to_qkeys = self.select_rubric_id_to_qkeys
            with open(filename, "w") as json_file:
                json.dump(rubric_id_to_qkeys, json_file)
            return rubric_id_to_qkeys
        else:
            with open(filename) as json_file:
                return json.load(json_file)

    def _question_key_to_total_pts(self, question_key: str, return_match:bool=False)->int:
        """

        Args:
            question_key: The name of the question on the CSV file

        Returns: How many points that question is worth (as inferred by the
        question key)
           if return_match, returns the whole match in the string

        """
        match = re.search(r'\((\d+(?:\.\d+)?)\s*pts\)', question_key)
        if match:
            if not return_match:
                return float(match.group(1))
            return match
        else:
            raise RuntimeError("Unable to find match")


    def get_rubric_id_to_total_pts(self, rubric_id_to_qkeys :dict)->dict[str, Union[int, float]]:
        """

        Args:
            rubric_id_to_qkeys: Dictionary mapping rubric IDs to question keys

        Returns: A dictionary mapping rubric ids
         to the total number of points possible (across all qkeys) for that rubric id

        """
        rubric_id_to_total_pts = {}
        for rubric_id in rubric_id_to_qkeys:
            rubric_id_to_total_pts[rubric_id] = 0
            for question_key in rubric_id_to_qkeys[rubric_id]:
                rubric_id_to_total_pts[rubric_id] += self._question_key_to_total_pts(question_key)
        return rubric_id_to_total_pts

    @property
    def select_rubric_id_to_qkeys(self)-> dict[str, dict]:
        """
        A minimal user interface that prompts the users for whether each of the
        subquestions for a given question correspond to a particular rubric outcome

        Returns: the rubric_id_to_qkeys for that assignment

        """
        rubric_url = f"{self.course.PAGE_URL}/courses/{self.course.COURSE_ID}/assignments/{self.assignment_id}?include[]=rubric&include[]=rubric_association"
        response = requests.get(rubric_url, headers=self.course.headers)
        response.raise_for_status()
        canvas_rubrics_data = response.json()
        rubric_id_to_rubric_data: dict[str, dict] = {}
        if "rubric" not in canvas_rubrics_data:
            raise RubricNotFoundError(self.assignment_id)
        for rubric in canvas_rubrics_data['rubric']:
            #print(rubric) # print description and some more info about it
            print(f"Rubric item description: {rubric['description']}")

            # print df questions and corresponding indices
            inferred_keys = self.infer_assignment_keys_from_df(self.score_df)
            keys_for_rubric_item = []
            # get the indices
            for i, question_key in enumerate(inferred_keys):
                print("##########################")
                print(f"Subquestion {i} \n")
                print(question_key)
                res = input("Does this key correspond to the above rubric item? (y/n) :")
                if res == "y":
                    keys_for_rubric_item.append(question_key)
                print("##########################")

            # wait for confirmation
            print("Confirm that these are the correct question keys")
            for question_key in keys_for_rubric_item:
                print(f"Confirming question key: {question_key}")
            print("Done: saving to the dictionary")
            rubric_id_to_rubric_data[rubric['id']] = keys_for_rubric_item
        return rubric_id_to_rubric_data




    def compute_new_outcome(self, sid:str, student_name:str, submission_url:str, verbose:bool=True) -> dict:
        student_df = find_student_df_by_SID(self.score_df, sid, student_name = student_name)
        if student_name is None:
            return None

        new_outcome = {
            "rubric_assessment": {}}
        comment = ""
        missing = student_df["Status"] == "Missing"
        if missing:
            new_outcome["submission"] = {"late_policy_status": "missing"}
            comment += "Gradescope submission missing"

        for rubric_id in self.rubric_id_to_qkeys:
            qkeys = self.rubric_id_to_qkeys[rubric_id]
            mastery_score = self.compute_mastery_score(rubric_id, qkeys, student_df)
            new_outcome["rubric_assessment"][str(rubric_id)] = {"points": mastery_score}
            missing_specs = []
            unscored_specs = []
            for qkey in qkeys:
                if qkey not in student_df:
                    print("Cannot find student data for key {qkey}")
                subscore = student_df[qkey]
                score_regex_match_for_key = self._question_key_to_total_pts(qkey, return_match=True)
                qkey_minus_pts = qkey[2:-len(score_regex_match_for_key.group(0))]
                if subscore < float(score_regex_match_for_key.group(1)):
                    missing_specs.append(qkey_minus_pts)
                if np.isnan(subscore):
                    unscored_specs.append(qkey_minus_pts)

        if verbose:
            print(f"{student_name} new outcome: {new_outcome}")
        if len(missing_specs) and not missing:
            comment += "Not yet. According to Gradescope, you haven't met the following specs:\n"
            missing_spec_comment = "\n".join(missing_specs + unscored_specs)
            comment += missing_spec_comment
            comment += "\nPlease check Gradescope to review your feedback. To revise your work for full credit, please follow the steps in Section 3.6 of the syllabus."

        if len(comment):
            new_outcome["comment"] = {"text_comment": comment}
        return new_outcome


    def compute_mastery_score(self, rubric_id:str, qkeys:list, student_df:pd.DataFrame):
        """

        Args:
            rubric_id: rubric_id corresponding to the outcome we want to calculate the score for
            qkeys: question keys from the df to use to calculate the mastery score
            student_df: Pandas DataFrame containing student data

        Returns:
            score : a number between 0 and 1 (usually a percentage of points)

        """
        total_mastery_score = 0
        for qkey in qkeys:
            if qkey not in student_df:
                print("Cannot find student data for key {qkey}")
            subscore = student_df[qkey]
            total_mastery_score += subscore
        score = total_mastery_score / self.rubric_id_to_total_pts[rubric_id]

        mastery_score: int = self.score_to_rubric_score(score)
        return mastery_score

    @abstractmethod
    def infer_assignment_keys_from_df(self, student_df:pd.DataFrame) -> list:
        """
        :param student_df:
        :return: Infers which columns of the student_df correspond to
        the assignment based on self.name
        """
        raise NotImplementedError()

    def compute_total_question_score(self, sid:int, student_name:str) -> Union[float, int]:
        student_df = find_student_df_by_SID(self.score_df, sid, student_name)
        total_question_score = 0
        inferred_keys = self.infer_assignment_keys_from_df(student_df)
        for qkey in inferred_keys:
            total_question_score += student_df[qkey]
        if np.isnan(total_question_score):
            total_question_score = 0
        return total_question_score



class ExamQuestion(LoadFromCSVAssignment):
    def infer_assignment_keys_from_df(self, student_df:pd.DataFrame) -> list:
        assignment_keys = []
        question_in_assignment_name = int(re.search(r'Question\s+(\d+)', self.name).group(1))
        for key in student_df.keys():
            if "pts" not in key:
                continue
            match = re.match(r'(\d+(?:\.\d+)?)\s*:', key)
            if match:
                question_in_key = int(match.group(1)[0])
                if question_in_key == question_in_assignment_name:
                    assignment_keys.append(key)
            else:
                raise RuntimeError("Unable to find match for total question")
        return assignment_keys

class MultiScoreMultiOutcomeAssignment(LoadFromCSVAssignment):
    """
    A class that can handle multiple scores and multiple outcomes
    Used for homeworks and labs
    """
    def infer_assignment_keys_from_df(self, student_df:pd.DataFrame) -> list:
        assignment_keys = []
        for key in student_df.keys():
            if "pts" not in key:
                continue
            assignment_keys.append(key)
        return assignment_keys


class SingleScoreSingleOutcomeAssignment(Assignment):

    @property
    def need_to_update_total_question_score(self)->bool:
        return False

    def compute_new_outcome(self, sid:str, student_name:str, submission_url:str, default_0=True):
        response = requests.get(submission_url, headers=self.course.headers)

        submission_data: dict = response.json()
        #if "score" not in submission_data: TODO delete this if not needed
        #    if not default_0:
        #        raise StudentSubmissionNotFoundError(f"Could not find submission: {submission_url}")

        # You can do this via Gradescope as well
        if "score" not in submission_data or submission_data["score"] is None:
            if default_0:
                score = 0
            else:
                raise StudentSubmissionNotFoundError(student_name)
        else:
            score = submission_data["score"]

        new_outcome = {
            "rubric_assessment": {}}

        rubric_url = f"{self.course.PAGE_URL}/courses/{self.course.COURSE_ID}/assignments/{self.assignment_id}?include[]=rubric&include[]=rubric_association"
        response = requests.get(rubric_url, headers=self.course.headers)
        response.raise_for_status()
        canvas_rubrics_data = response.json()
        if "rubric" not in canvas_rubrics_data:
            raise RubricNotFoundError(self.assignment_id)

        for rubric in canvas_rubrics_data['rubric']:
            mastery_score = self.score_to_rubric_score(score/canvas_rubrics_data["points_possible"])

            new_outcome["rubric_assessment"][str(rubric["id"])] = {"points": mastery_score}
        print(f"{student_name} new outcome: {new_outcome}")
        return new_outcome


def make_assignment_from_name(assignment_name, assignment_id, course) -> Assignment:
    """

    Args:
        assignment_name: Name (must contain Lab, Homework Exam or Test as is on Canvas)
        assignment_id: Canvas assignment ID
        course: Course that contains the assignments

    Returns: Assignment object corresponding of the type inferred by the name

    """
    assignment_dir = course.course_config_root / f"assignment_{assignment_id}"
    possible_classes = ["ExamQuestion", "MultiScoreMultiOutcomeAssignment", "SingleScoreSingleOutcomeAssignment"]
    if not os.path.exists(assignment_dir / "assignment.json"):
        os.makedirs(assignment_dir, exist_ok=True)
        assignment_cls_input = input(f"{assignment_name} Assignment class : SS, EQ or {possible_classes}: ")
        if assignment_cls_input == "SS":
            assignment_cls_input = "SingleScoreSingleOutcomeAssignment" #shorthand
        if assignment_cls_input == "EQ":
            assignment_cls_input = "ExamQuestion" #shorthand
        assert assignment_cls_input in possible_classes
        data_dict = {"assignment_cls": assignment_cls_input}
        with open(assignment_dir / "assignment.json", 'w') as fp:
            json.dump(data_dict, fp)
    else:
        with open(assignment_dir / "assignment.json", 'r', encoding='utf-8') as file:
            data_dict = json.load(file)
    assignment_cls = eval(data_dict["assignment_cls"])
    assignment = assignment_cls(assignment_name, assignment_id, course)
    return assignment

