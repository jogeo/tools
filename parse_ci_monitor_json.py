#!/usr/bin/env python3

import argparse
import http.client
import json
import os
import re
import sys
import subprocess
import urllib.request

from datetime import datetime
from http.client import IncompleteRead
from typing import List


class IssuesFinder:
    """Known OCP CI issues."""

    def __init__(self):
        self.issues_found = []

    def get_issue_methods(self):
        names = [name for name in dir(self) if name.startswith("issue_")]
        return [getattr(self, name) for name in names]

    def find_issues(self, test_info):
        if test_info["logs"] is not None:
            log_text = self.get_file_from_url(test_info["logs"])
            print(f"Checking for known issue.")
            for issue_method in self.get_issue_methods():
                known_issue = issue_method(log_text)
                if known_issue is not None:
                    self.issues_found.append(known_issue)
        return self.issues_found

    def get_file_from_url(self, url: str):
        "Return file content downloaded from url."

        for i in range(3):
            try:
                content = urllib.request.urlopen(url).read().decode("utf8")
            except IncompleteRead:
                print(f"Caught IncompleteRead in iteration {i}.")
                continue
            else:
                break
        return content

    def match_ordered_strings(self, log: str, targets: list):
        """Returns True if all targets are found, otherwise False."""
        found_all = set()
        for target in targets:
            line_count = 0
            found = False
            for line in log.splitlines():
                line_count += 1
                match = re.search(target, line)
                if match:
                    found = True
                    break
            found_all.add(found)
        return all(found_all)

    def issue_fail_to_get_project_during_cleanup(self, log_text):
        re_strings = [
            r"=== After Scenario:",
            r"the server is currently unable to handle the request \(get projects.project.openshift.io\)",
            r"RuntimeError: error getting projects by user",
        ]

        if self.match_ordered_strings(log_text, re_strings):
            return "After scenario RuntimeError raised while getting project during cleanup."


def argparser():
    parser = argparse.ArgumentParser(
        prog="This tool helps to group failed tests based on owners, feature tests and testcase ids across multiple nightly or upgrade runs "
    )

    parser.add_argument(
        "-r",
        "--runs",
        nargs="+",
        help="space seperated list of test run ids.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="location to write output file",
        default=f"./{datetime.today().strftime('%Y%m%d')}.json",
    )
    parser.add_argument("-v", "--version", help="Specify OCP version", required=True)
    return parser.parse_args()


def get_link_to_log(content: str):
    content = re.search("(http.*)", content)
    if content:
        return content.groups()[0]


def get_automation_script(cfields: List):
    for cf in cfields:
        if cf["key"] == "automation_script":
            script = cf["value"]["content"]
            m = re.search("file: (features.*feature)\\n", script)
            automation_script = m.groups()[0]
            break
    return automation_script


def get_owner(ascript: str, testid: str):
    BUSHSLICER_HOME = os.getenv("BUSHSLICER_HOME")
    dir = f"{BUSHSLICER_HOME}/{ascript}"
    owner = None
    try:
        owner = subprocess.check_output(
            f"egrep -B 1 {testid} {dir} | grep author", shell=True
        )
        owner = owner.decode().rstrip()
        owner = re.search("author\s*(.*)@redhat.com", owner)
        owner = owner.groups()[0]
    except subprocess.CalledProcessError:
        owner = "Not found"
    return owner


def get_testrun_json(run_id: str):
    """Download the test case json data from polarshift."""

    # Call $BUSHSLICER_HOME/tools/polarshift.rb get-run RUN_ID to download the json describing the test run
    BUSHSLICER_HOME = os.getenv("BUSHSLICER_HOME")
    # use -o to avoid extra non json garbage printed to stdout
    cmd = [
        f"{BUSHSLICER_HOME}/tools/polarshift.rb",
        "get-run",
        f"{run_id}",
        "-o",
        f"{run_id}.json",
    ]
    subprocess.check_output(cmd)
    run_json = get_json_from_file(f"{run_id}.json")
    return run_json


def get_json_from_file(file_path: str):
    """Read in json from file_path."""

    with open(file_path, "r") as f:
        content = json.load(f)
    return content


def write_output(data: dict, ofile: str):
    with open(ofile, "w") as outfile:
        json.dump(data, outfile, indent=4, sort_keys=True)


def main():
    args = argparser()
    # logs_files = set(args.files)
    report_struct = {"version": args.version}
    # issue_finder = IssuesFinder()
    # issue_finder.get_issue_methods()
    # $BUSHSLICER_HOME/tools/polarshift.rb get-run 20210909-0851 -o 20210909-0851.json
    for run in args.runs:
        output = get_testrun_json(run)
        profile = output["title"]
        profile = re.search(".* - (.*)$", profile)
        profile = profile.groups()[0]
        for record in output["records"]["TestRecord"]:
            if record["result"] == "Failed":
                linkto_logs = get_link_to_log(record["comment"]["content"])
                automation_script = get_automation_script(
                    record["test_case"]["customFields"]["Custom"]
                )
                id = record["test_case"]["id"]
                owner = get_owner(automation_script, id)
                failed_test_attrs = dict(
                    [
                        ("case", id),
                        ("logs", linkto_logs),
                        ("profile", profile),
                    ]
                )

                # Find known issues
                issue_finder = IssuesFinder()
                issues = issue_finder.find_issues(failed_test_attrs)
                if issues:
                    failed_test_attrs["known_issues"] = issues

                if report_struct.get(owner, 0):
                    if report_struct[owner].get(automation_script, 0):
                        if report_struct[owner][automation_script].get(id, 0):
                            report_struct[owner][automation_script][
                                id
                            ] = failed_test_attrs
                        else:
                            report_struct[owner][automation_script].update(
                                {id: failed_test_attrs}
                            )
                    else:
                        report_struct[owner].update(
                            {automation_script: {id: failed_test_attrs}}
                        )
                else:
                    report_struct[owner] = {automation_script: {id: failed_test_attrs}}

    write_output(report_struct, args.output)


if __name__ == "__main__":
    sys.exit(main())
