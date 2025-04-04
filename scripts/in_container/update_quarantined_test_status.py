#!/usr/bin/env python
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import jinja2
from bs4 import BeautifulSoup
from github3 import login
from jinja2 import StrictUndefined
from tabulate import tabulate


class TestResult(NamedTuple):
    test_id: str
    file: str
    name: str
    classname: str
    line: str
    result: bool


class TestHistory(NamedTuple):
    test_id: str
    name: str
    url: str
    states: list[bool]
    comment: str


test_results = []

user = ""
repo = ""
issue_id = 0
num_runs = 10

url_pattern = re.compile(r"\[([^]]*)]\(([^)]*)\)")

status_map: dict[str, bool] = {
    ":heavy_check_mark:": True,
    ":x:": False,
}

reverse_status_map: dict[bool, str] = {val: key for key, val in status_map.items()}


def get_url(result: TestResult) -> str:
    return (
        f"[{result.name}](https://github.com/{user}/{repo}/blob/"
        f"main/{result.file}?test_id={result.test_id}#L{result.line})"
    )


def parse_state_history(history_string: str) -> list[bool]:
    history_array = history_string.split(" ")
    status_array: list[bool] = []
    for value in history_array:
        if value:
            status_array.append(status_map[value])
    return status_array


def parse_test_history(line: str) -> TestHistory | None:
    values = line.split("|")
    match_url = url_pattern.match(values[1].strip())
    if match_url:
        name = match_url.group(1)
        url = match_url.group(0)
        http_url = match_url.group(2)
        parsed_url = urlsplit(http_url)
        the_id = parsed_url[3].split("=")[1]
        comment = values[5] if len(values) >= 6 else ""
        try:
            states = parse_state_history(values[3])
        except Exception:
            states = []
        return TestHistory(
            test_id=the_id,
            name=name,
            states=states,
            url=url,
            comment=comment,
        )
    return None


def parse_body(body: str) -> dict[str, TestHistory]:
    parse = False
    test_history_map: dict[str, TestHistory] = {}
    for line in body.splitlines(keepends=False):
        if line.startswith("|-"):
            parse = True
        elif parse:
            if not line.startswith("|"):
                break
            try:
                status = parse_test_history(line)
            except Exception:
                continue
            else:
                if status:
                    test_history_map[status.test_id] = status
    return test_history_map


def update_test_history(history: TestHistory, last_status: bool):
    print(f"Adding status to test history: {history}, {last_status}")
    return TestHistory(
        test_id=history.test_id,
        name=history.name,
        url=history.url,
        states=([last_status] + history.states)[0:num_runs],
        comment=history.comment,
    )


def create_test_history(result: TestResult) -> TestHistory:
    print(f"Creating test history {result}")
    return TestHistory(
        test_id=result.test_id, name=result.name, url=get_url(result), states=[result.result], comment=""
    )


def get_history_status(history: TestHistory):
    if len(history.states) < num_runs:
        if all(history.states):
            return "So far, so good"
        return "Flaky"
    if all(history.states):
        return "Stable"
    if all(history.states[0 : num_runs - 1]):
        return "Just one more"
    if all(history.states[0 : num_runs // 2]):
        return "Almost there"
    return "Flaky"


def get_table(history_map: dict[str, TestHistory]) -> str:
    headers = ["Test", "Last run", f"Last {num_runs} runs", "Status", "Comment"]
    the_table: list[list[str]] = []
    for _, history in sorted(history_map.items()):
        the_table.append(
            [
                history.url,
                "Succeeded" if history.states[0] else "Failed",
                " ".join(reverse_status_map[state] for state in history.states),
                get_history_status(history),
                history.comment,
            ]
        )
    return tabulate(the_table, headers, tablefmt="github")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Provide XML JUNIT FILE as first argument")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        text = f.read()
    y = BeautifulSoup(text, "html.parser")
    testsuites = y.testsuites
    if testsuites is None:
        print("No testsuites found in the XML file")
        sys.exit(1)
    testsuite = testsuites.testsuite
    if testsuite is None:
        print(f"No testsuite found in the XML file in {testsuites}")
        sys.exit(1)
    res = testsuite.findAll("testcase")  # type: ignore[call-arg]
    for test in res:
        print("Parsing: " + test["classname"] + "::" + test["name"])
        if test.contents and test.contents[0].name == "skipped":
            print(f"skipping {test['name']}")
        else:
            test_results.append(
                TestResult(
                    test_id=test["classname"] + "::" + test["name"],
                    file=test["file"],
                    line=test["line"],
                    name=test["name"],
                    classname=test["classname"],
                    result=not test.contents,
                )
            )

    token = os.environ.get("GITHUB_TOKEN")
    print(f"Token: {token}")
    github_repository = os.environ.get("GITHUB_REPOSITORY")
    if not github_repository:
        raise RuntimeError("GitHub Repository must be defined!")
    user, repo = github_repository.split("/")
    print(f"User: {user}, Repo: {repo}")
    issue_id = int(os.environ.get("ISSUE_ID", 0))
    num_runs = int(os.environ.get("NUM_RUNS", 10))

    if issue_id == 0:
        raise RuntimeError("You need to define ISSUE_ID as environment variable")

    gh = login(token=token)

    quarantined_issue = gh.issue(user, repo, issue_id)
    print("-----")
    print(quarantined_issue.body)
    print("-----")
    parsed_test_map = parse_body(quarantined_issue.body)
    new_test_map: dict[str, TestHistory] = {}

    for test_result in test_results:
        previous_results = parsed_test_map.get(test_result.test_id)
        if previous_results:
            updated_results = update_test_history(previous_results, test_result.result)
            new_test_map[previous_results.test_id] = updated_results
        else:
            new_history = create_test_history(test_result)
            new_test_map[new_history.test_id] = new_history
    table = get_table(new_test_map)
    print()
    print("Result:")
    print()
    print(table)
    print()
    with Path(__file__).resolve().with_name("quarantine_issue_header.md").open() as f:
        header = jinja2.Template(f.read(), autoescape=True, undefined=StrictUndefined).render(
            DATE_UTC_NOW=datetime.now(tz=timezone.utc).isoformat("T", timespec="seconds")
        )
    quarantined_issue.edit(
        title=None, body=f"{header}\n\n{table}", state="open" if test_results else "closed"
    )
