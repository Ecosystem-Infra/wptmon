#!/usr/bin/env python3
# Lint as: python3
"""Placeholder main

This will monitor some wpt jobs eventually
"""
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from datetime import timezone

import flask
import requests
from dateutil import parser as date_parser
from github import Github
from google.api_core import exceptions as g_exceptions
from google.cloud import firestore
from google.cloud import monitoring_v3

logging.basicConfig(level=logging.DEBUG)
app = flask.Flask(__name__)

GCLOUD_PROJECT_ID = "wptmon"
DESCRIPTOR_PROJECT_NAME = "projects/wptmon"
RUNS_REQ_TIMEOUT_SECONDS = 5
RUNS_MAX_AGE_SECONDS = 24 * 60 * 60  # 1 day
OPEN_PRS_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 1 week
ERROR_RESULT = -1

RECENT_STABLE_RUNS_FETCH_URL = "https://wpt.fyi/api/runs?labels=master,stable&products=chrome,firefox,safari&max-count=1"
RECENT_EXPERIMENTAL_RUNS_FETCH_URL = "https://wpt.fyi/api/runs?labels=master,experimental&products=chrome,edge,firefox,safari,webkitgtk&max-count=1"

METRIC_TYPE_RECENT_STABLE_RUNS = "custom.googleapis.com/wpt.fyi/recent_stable_runs"
METRIC_TYPE_RECENT_EXPERIMENTAL_RUNS = "custom.googleapis.com/wpt.fyi/recent_experimental_runs"
METRIC_TYPE_OPEN_PRS = "custom.googleapis.com/wpt-github/open_prs"
METRIC_TYPE_OPEN_PR_STATUSES = "custom.googleapis.com/wpt-github/open_pr_status"
METRIC_TYPE_OPEN_PR_NO_CHECKS = "custom.googleapis.com/wpt-github/open_pr_no_checks"


class MonitorState():
  """This is a state object for holding artifacts during metrics gathering.

  Many of the APIs we call to get metrics data require HTTP calls or
  heavy-weight processing, so it's useful to be able to save intermediate
  data from processing one metric to later use for processing another metric.
  """
  def __init__(self):
    # The github.PaginatedList.PaginatedList object containing the set of open
    # pull requests from github. Note that accessing this issues more HTTP
    # requests.
    self.github_open_prs = None


def _get_github_token():
  db = firestore.Client()
  doc_ref = db.collection("config").document("github").get()
  if doc_ref.exists:
    return doc_ref.to_dict()["gh_token"]
  return None


def _create_metric_recent_runs(type, description):
  """Creates a recent runs metric (either stable or experimental).

  It's OK to repeatedly create the same metric as long as none of the parameters
  change. In such a case, re-creation is a no-op. If anything does change then
  re-creation is an error (because we use the same name), and we have to delete
  the old metric before creating it again.

  :arg type: string descriptor type for the metric
  :arg description: string human-readable description of what the metric is
  """
  client = monitoring_v3.MetricServiceClient()
  descriptor = monitoring_v3.types.MetricDescriptor()
  descriptor.type = type
  descriptor.value_type = monitoring_v3.enums.MetricDescriptor.ValueType.INT64
  descriptor.metric_kind = monitoring_v3.enums.MetricDescriptor.MetricKind.GAUGE
  descriptor.description = description
  client.create_metric_descriptor(DESCRIPTOR_PROJECT_NAME, descriptor)


def _write_metric_recent_runs(type, browser_name, num_recent_runs, ):
  """Updates a 'recent runs' metric (stable or experimental).

  :arg type: string descriptor type for the metric
  :arg browser_name: string name of the browser, added as a label for the metric
  :arg num_recent_runs: int the number of recent runs
  """
  client = monitoring_v3.MetricServiceClient()
  series = monitoring_v3.types.TimeSeries()
  series.metric.type = type
  series.resource.type = "generic_task"
  series.resource.labels["namespace"] = "wpt"
  series.resource.labels["location"] = "us-east1"
  series.resource.labels["job"] = "wpt.fyi"
  # We stick the browser name inside the task ID. Unfortunately we can't modify
  # the set of labels that is available on the MonitoredResource, nor can we
  # define a custom one.
  series.resource.labels["task_id"] = browser_name
  point = series.points.add()
  point.value.int64_value = num_recent_runs
  point.interval.end_time.seconds = int(time.time())
  client.create_time_series(DESCRIPTOR_PROJECT_NAME, [series])


def create_and_write_metric_open_prs(num_open_prs):
  client = monitoring_v3.MetricServiceClient()
  descriptor = monitoring_v3.types.MetricDescriptor()
  descriptor.type = METRIC_TYPE_OPEN_PRS
  descriptor.value_type = monitoring_v3.enums.MetricDescriptor.ValueType.INT64
  descriptor.metric_kind = monitoring_v3.enums.MetricDescriptor.MetricKind.GAUGE
  descriptor.description = "Number of open Github pull requests in the WPT repository"
  client.create_metric_descriptor(DESCRIPTOR_PROJECT_NAME, descriptor)

  series = monitoring_v3.types.TimeSeries()
  series.metric.type = METRIC_TYPE_OPEN_PRS
  series.resource.type = "generic_task"
  series.resource.labels["namespace"] = "wpt"
  series.resource.labels["location"] = "us-east1"
  series.resource.labels["job"] = "wpt-github"
  series.resource.labels["task_id"] = "wpt-github"
  point = series.points.add()
  point.value.int64_value = num_open_prs
  point.interval.end_time.seconds = int(time.time())
  client.create_time_series(DESCRIPTOR_PROJECT_NAME, [series])


def create_and_write_metric_open_pr_no_checks(num_prs_with_no_checks, is_draft):
  client = monitoring_v3.MetricServiceClient()
  descriptor = monitoring_v3.types.MetricDescriptor()
  descriptor.type = METRIC_TYPE_OPEN_PR_NO_CHECKS
  descriptor.value_type = monitoring_v3.enums.MetricDescriptor.ValueType.INT64
  descriptor.metric_kind = monitoring_v3.enums.MetricDescriptor.MetricKind.GAUGE
  descriptor.description = "Number of open Github pull requests with no checks"
  client.create_metric_descriptor(DESCRIPTOR_PROJECT_NAME, descriptor)

  series = monitoring_v3.types.TimeSeries()
  series.metric.type = METRIC_TYPE_OPEN_PR_NO_CHECKS
  series.resource.type = "generic_task"
  series.resource.labels["namespace"] = "wpt"
  series.resource.labels["location"] = "us-east1"
  series.resource.labels["job"] = "wpt-github"
  series.resource.labels["task_id"] = "draft_pr" if is_draft else "pr"
  point = series.points.add()
  point.value.int64_value = num_prs_with_no_checks
  point.interval.end_time.seconds = int(time.time())
  client.create_time_series(DESCRIPTOR_PROJECT_NAME, [series])


def create_metric_open_pr_statuses():
  client = monitoring_v3.MetricServiceClient()
  descriptor = monitoring_v3.types.MetricDescriptor()
  descriptor.type = METRIC_TYPE_OPEN_PR_STATUSES
  descriptor.value_type = monitoring_v3.enums.MetricDescriptor.ValueType.INT64
  descriptor.metric_kind = monitoring_v3.enums.MetricDescriptor.MetricKind.GAUGE
  descriptor.description = "Check statuses for open pull requests in the WPT repository"
  client.create_metric_descriptor(DESCRIPTOR_PROJECT_NAME, descriptor)


def write_metric_open_pr_statuses(check_name, check_status, count):
  """Writes a single point to the 'open pr statuses' metric.

  :arg check_name: str the name of the check that ran on the PR
  :arg check_status: str the status of the check (eg: success, failure, error)
  :arg count: int the number of PRs that had this check with this status.
  """
  client = monitoring_v3.MetricServiceClient()
  series = monitoring_v3.types.TimeSeries()
  series.metric.type = METRIC_TYPE_OPEN_PR_STATUSES
  series.resource.type = "generic_task"
  series.resource.labels["namespace"] = "wpt"
  series.resource.labels["location"] = "us-east1"
  # We are mis-using labels here because cloud monitoring won't let us define
  # our own labels.
  series.resource.labels["job"] = check_name
  series.resource.labels["task_id"] = check_status
  point = series.points.add()
  point.value.int64_value = count
  point.interval.end_time.seconds = int(time.time())
  client.create_time_series(DESCRIPTOR_PROJECT_NAME, [series])


def create_metric_recent_stable_runs():
  """This creates a metric for tracking recent stable runs."""
  _create_metric_recent_runs(METRIC_TYPE_RECENT_STABLE_RUNS,
                             "Number of recent stable runs on wpt.fyi")


def create_metric_recent_experimental_runs():
  """This creates a metric for tracking recent experimental runs."""
  _create_metric_recent_runs(METRIC_TYPE_RECENT_EXPERIMENTAL_RUNS,
                             "Number of recent experimental runs on wpt.fyi")


def write_metric_recent_stable_runs(browser_name, num_stable_runs):
  """Updates the 'recent stable runs' metric.

  Sets the number of recent stable runs for the specified browser.
  """
  _write_metric_recent_runs(METRIC_TYPE_RECENT_STABLE_RUNS, browser_name,
                            num_stable_runs)


def write_metric_recent_experimental_runs(browser_name, num_experimental_runs):
  """Updates the 'recent experimental runs' metric.

   Sets the the number of recent experimental runs for the specified browser.
   """
  _write_metric_recent_runs(METRIC_TYPE_RECENT_EXPERIMENTAL_RUNS,
                            browser_name, num_experimental_runs)


def _get_recent_runs(fetch_url, max_age_seconds):
  """Get a list of latest runs and identify which browsers have recent runs.

  :arg fetch_url: string url where to fetch the list of runs
  :arg max_age: int the age, in seconds, of the oldest run that is considered recent.
  :return list of strings containing the browser names with a recent run, or None
          if recent runs could not be fetched.
  """

  try:
    fyi_runs = requests.get(fetch_url, timeout=RUNS_REQ_TIMEOUT_SECONDS)
    # This will raise an exception if there was an error code in the response,
    # or do nothing if the request was successful.
    fyi_runs.raise_for_status()
  except requests.exceptions.RequestException as e:
    logging.error("Failed to fetch runs, e=%s" % e)
    return None

  try:
    if not fyi_runs.json():
      logging.error("Received empty JSON")
      return None
  except:
    logging.error("Failed to parse JSON, content=%s" % fyi_runs.content)
    return None

  # At this point we should have a response in |fyi_runs|, which is a list of
  # dicts. Each dict contains info about a single run, one for each browser.
  recent_browsers = []
  for run in fyi_runs.json():
    logging.debug("Processing run %s" % json.dumps(run))
    run_end_time = date_parser.parse(run["time_end"])
    run_age_seconds = (datetime.now(tz=timezone.utc) - run_end_time).total_seconds()
    if run_age_seconds <= max_age_seconds:
      recent_browsers.append(run["browser_name"])

  logging.info("Finished processing runs, recent browser runs=%s" % recent_browsers)
  return recent_browsers


def get_recent_stable_runs():
  logging.info("Finding recent stable runs")
  recent_browsers = _get_recent_runs(RECENT_STABLE_RUNS_FETCH_URL, RUNS_MAX_AGE_SECONDS)
  if recent_browsers is None:
    return "Failed to get stable runs"

  try:
    create_metric_recent_stable_runs()
    for browser_name in recent_browsers:
      write_metric_recent_stable_runs(browser_name, 1)
  except g_exceptions.InvalidArgument as e:
    logging.error("Failed to update stable run metrics skipping: %s" % e)
  return "Recent stable runs: %s" % recent_browsers


def get_recent_experimental_runs():
  logging.info("Finding recent experimental runs")
  recent_browsers = _get_recent_runs(RECENT_EXPERIMENTAL_RUNS_FETCH_URL, RUNS_MAX_AGE_SECONDS)
  if recent_browsers is None:
    return "Failed to get experimental runs"

  try:
    create_metric_recent_experimental_runs()
    for browser_name in recent_browsers:
      write_metric_recent_experimental_runs(browser_name, 1)
  except g_exceptions.InvalidArgument as e:
    logging.error("Failed to update experimental run metrics skipping: %s" % e)
  return "Recent experimental runs: %s" % recent_browsers


def get_num_open_prs(mon_state):
  logging.info("Counting open github PRs")
  gh_token = _get_github_token()
  if not gh_token:
    return "Failed to load github token"

  github = Github(gh_token)
  wpt_repo = github.get_repo("web-platform-tests/wpt")
  mon_state.github_open_prs = wpt_repo.get_pulls(state="open", base="master",
                                                 sort="updated", direction="desc")
  num_open_prs = mon_state.github_open_prs.totalCount
  logging.info("Finished counting open PRs: %d" % num_open_prs)

  try:
    create_and_write_metric_open_prs(num_open_prs)
  except g_exceptions.InvalidArgument as e:
    logging.error("Failed to update open PRs metric, skipping: %s" % e)
  return "Open PRs: %d" % num_open_prs


def get_open_pr_statuses(mon_state):
  logging.info("Finding statuses for open PRs")
  # We require that open PRs have already been identified.
  # TODO: make this capable of rebuilding open PRs list if needed
  assert mon_state.github_open_prs is not None

  # Status types is a 2-level dict mapping context->state->count, making it a
  # dict(str->dict(str->int))
  status_dict = defaultdict(lambda: defaultdict(int))
  # Keep track of the PR numbers that have no checks.
  prs_with_no_checks = []
  draft_prs_with_no_checks = []

  for open_pr in mon_state.github_open_prs:
    # The timestamps reported by github are in UTC, so add the tzinfo.
    pr_update_time_utc = open_pr.updated_at.replace(tzinfo=timezone.utc)
    pr_age_seconds = (datetime.now(tz=timezone.utc) - pr_update_time_utc).total_seconds()
    if pr_age_seconds >= OPEN_PRS_MAX_AGE_SECONDS:
      logging.info("PR %d last updated on %s which exceeds the max, skipping the rest"
                   % (open_pr.number, open_pr.updated_at.strftime("%c%Z")))
      break

    # Commits are listed in order of increasing timestamp, meaning the last one
    # is the most-recent one.
    latest_commit = open_pr.get_commits()[open_pr.commits - 1]
    # We try to dedupe the statuses for a given commit. This API seems to list
    # the history of statuses for this commit, so you often see a "pending" check
    # followed by that same check in a "success" state. We only want to keep the
    # success in that example.
    # Statuses are listed in reverse-chronological order, with the latest at the
    # top of the list, so we traverse backwards to follow the timeline of events
    # and overwrite status for any check that repeats.
    deduped_dict = defaultdict(str)
    for commit_status in latest_commit.get_statuses().reversed:
      deduped_dict[commit_status.context] = commit_status.state
    logging.info("PR %d has check status %s" % (open_pr.number, deduped_dict))
    if not deduped_dict:
      # Empty dict means there are no checks on this PR
      if open_pr.draft:
        draft_prs_with_no_checks.append(open_pr.number)
      else:
        prs_with_no_checks.append(open_pr.number)
    # Add the deduped statuses for this PR to the combined list
    for context, state in deduped_dict.items():
      status_dict[context][state] += 1

  try:
    create_and_write_metric_open_pr_no_checks(len(prs_with_no_checks), is_draft=False)
    create_and_write_metric_open_pr_no_checks(len(draft_prs_with_no_checks), is_draft=True)
  except g_exceptions.InvalidArgument as e:
    logging.error("Failed to PRs-with-no-checks metric, skipping: %s" % e)

  create_metric_open_pr_statuses()
  for context, state_dict in status_dict.items():
    for state, count in state_dict.items():
      print("Trying to write a metric for %s %s %d" % (context, state, count))
      try:
        write_metric_open_pr_statuses(context, state, count)
      except g_exceptions.InvalidArgument as e:
        logging.error("Failed to PR status metric, skipping: %s" % e)

  logging.info("Got combined PR statuses, dict=%s", status_dict)
  logging.info("PRs with no checks, %s", prs_with_no_checks)
  logging.info("Draft PRs with no checks, %s", draft_prs_with_no_checks)
  return "Open PR statuses: %s<br>\nOpen PRs with no checks: [%d] %s Drafts: [%d] %s" \
         % (status_dict, len(prs_with_no_checks), prs_with_no_checks,
            len(draft_prs_with_no_checks), draft_prs_with_no_checks)

@app.route("/")
def get_all_metrics():
  mon_state = MonitorState()
  result = get_recent_stable_runs()
  result += "<br>\n" + get_recent_experimental_runs()
  result += "<br>\n" + get_num_open_prs(mon_state)
  result += "<br>\n" + get_open_pr_statuses(mon_state)
  return result


def main(argv):
  # This method is only used when running locally
  if len(argv) > 2:
    raise app.UsageError("Too many command-line arguments.")
  if "localhost" in argv:
    app.run(host='127.0.0.1', port=8080, debug=True)
    return
  result = get_all_metrics()
  print("Finished main, result below\n%s" % result)


if __name__ == '__main__':
  main(sys.argv[1:])
