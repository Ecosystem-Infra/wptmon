#!/usr/bin/env python3
# Lint as: python3
"""Placeholder main

This will monitor some wpt jobs eventually
"""
from datetime import datetime, timezone
from dateutil import parser as date_parser
import json
import logging
import requests
import sys
import time

import flask
from google.api_core import exceptions as g_exceptions
from google.cloud import monitoring_v3

logging.basicConfig(level=logging.DEBUG)
app = flask.Flask(__name__)

GCLOUD_PROJECT_ID = "wptmon"
DESCRIPTOR_PROJECT_NAME = "projects/wptmon"
RUNS_REQ_TIMEOUT_SECONDS = 5
RUNS_MAX_AGE_SECONDS = 24 * 60 * 60  # 1 day

RECENT_STABLE_RUNS_FETCH_URL="https://wpt.fyi/api/runs?labels=master,stable&products=chrome,firefox,safari&max-count=1"
RECENT_EXPERIMENTAL_RUNS_FETCH_URL="https://wpt.fyi/api/runs?labels=master,experimental&products=chrome,edge,firefox,safari,webkitgtk&max-count=1"

METRIC_TYPE_RECENT_STABLE_RUNS = "custom.googleapis.com/wpt.fyi/recent_stable_runs"
METRIC_TYPE_RECENT_EXPERIMENTAL_RUNS = "custom.googleapis.com/wpt.fyi/recent_experimental_runs"

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

def _write_metric_recent_runs(type, num_recent_runs):
  """Updates a 'recent runs' metric (stable or experimental).

  :arg type: string descriptor type for the metric
  :arg num_recent_runs: int the number of recent runs
  """
  client = monitoring_v3.MetricServiceClient()
  series = monitoring_v3.types.TimeSeries()
  series.metric.type = type
  series.resource.type = "generic_task"
  series.resource.labels["namespace"] = "wpt"
  series.resource.labels["location"] = "us-east1"
  series.resource.labels["job"] = "wpt.fyi"
  series.resource.labels["task_id"] = "wpt.fyi"
  point = series.points.add()
  point.value.int64_value = num_recent_runs
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

def write_metric_recent_stable_runs(num_stable_runs):
  """Updates the 'recent stable runs' metric with the number of stable runs."""
  _write_metric_recent_runs(METRIC_TYPE_RECENT_STABLE_RUNS, num_stable_runs)

def write_metric_recent_experimental_runs(num_experimental_runs):
  """Updates the 'recent experimental runs' metric with the number of stable runs."""
  _write_metric_recent_runs(METRIC_TYPE_RECENT_EXPERIMENTAL_RUNS, num_experimental_runs)

def _get_num_recent_runs(fetch_url, max_age_seconds):
  """Get a list of latest runs and count how many are recent.

  :arg fetch_url: string url where to fetch the list of runs
  :arg max_age: int the age, in seconds, of the oldest run that is considered recent.
  :return int the number of recent runs
  """

  try:
    fyi_runs = requests.get(fetch_url, timeout=RUNS_REQ_TIMEOUT_SECONDS)
    # This will raise an exception if there was an error code in the response,
    # or do nothing if the request was successful.
    fyi_runs.raise_for_status()
  except requests.exceptions.RequestException as e:
    logging.error("Failed to fetch runs, e=%s" % e)
    return "Failed to fetch runs, e=%s" % e

  try:
    if not fyi_runs.json():
      logging.error("Received empty JSON")
      return "Received empty JSON"
  except:
    logging.error("Failed to parse JSON, content=%s" % fyi_runs.content)
    return "Failed to parse JSON, content=%s" % fyi_runs.content

  # At this point we should have a response in |fyi_runs|, which is a list of
  # dicts. Each dict contains info about a single run.
  # Count the number of recent runs.
  recent_run_count = 0
  for run in fyi_runs.json():
    logging.debug("Processing run %s" % json.dumps(run))
    run_end_time = date_parser.parse(run["time_end"])
    run_age_seconds = (datetime.now(tz=timezone.utc) - run_end_time).total_seconds()
    if run_age_seconds <= max_age_seconds:
      recent_run_count += 1

  logging.info("Finished processing runs, recent runs=%d" % recent_run_count)
  return recent_run_count

def get_num_recent_stable_runs():
  logging.info("Counting recent stable runs")
  num_recent_runs = _get_num_recent_runs(RECENT_STABLE_RUNS_FETCH_URL, RUNS_MAX_AGE_SECONDS)
  try:
    create_metric_recent_stable_runs()
    write_metric_recent_stable_runs(num_recent_runs)
  except g_exceptions.InvalidArgument as e:
    logging.error("Failed to update stable run metrics skipping: %s" % e)
  return "Recent stable runs: %d" % num_recent_runs

def get_num_recent_experimental_runs():
  logging.info("Counting recent experimental runs")
  num_recent_runs = _get_num_recent_runs(RECENT_EXPERIMENTAL_RUNS_FETCH_URL, RUNS_MAX_AGE_SECONDS)
  try:
    create_metric_recent_experimental_runs()
    write_metric_recent_experimental_runs(num_recent_runs)
  except g_exceptions.InvalidArgument as e:
    logging.error("Failed to update experimental run metrics skipping: %s" % e)
  return "Recent experimental runs: %d" % num_recent_runs

@app.route("/")
def get_num_recent_runs():
  result = get_num_recent_stable_runs()
  result += "<br>\n" + get_num_recent_experimental_runs()
  return result

def main(argv):
  # This method is only used when running locally
  if len(argv) > 2:
    raise app.UsageError("Too many command-line arguments.")
  if "localhost" in argv:
    app.run(host='127.0.0.1', port=8080, debug=True)
    return
  get_num_recent_runs()

if __name__ == '__main__':
  main(sys.argv[1:])
