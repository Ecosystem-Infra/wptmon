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

import flask

logging.basicConfig(level=logging.DEBUG)
app = flask.Flask(__name__)

RUNS_REQ_TIMEOUT_SECONDS = 5
RUNS_MAX_AGE_SECONDS = 24 * 60 * 60  # 1 day

@app.route("/")
def get_num_recent_stable_runs():
  try:
    fyi_runs = requests.get("https://wpt.fyi/api/runs?labels=master,stable&products=chrome,firefox,safari&max-count=1", timeout=RUNS_REQ_TIMEOUT_SECONDS)
    # This will raise an exception if there was an error code in the response,
    # or do nothing if the request was successful.
    fyi_runs.raise_for_status()
  except requests.exceptions.RequestException as e:
    logging.error("Failed to fetch runs, e=%s" % e)
    return -1

  try:
    if not fyi_runs.json():
      logging.error("Received empty JSON")
      return -1
  except:
    logging.error("Failed to parse JSON, content=%s" % fyi_runs.content)
    return -1

  # At this point we should have a response in |fyi_runs|, which is a list of
  # dicts. Each dict contains info about a single run.
  # Count the number of recent runs.
  recent_run_count = 0
  for run in fyi_runs.json():
    logging.debug("Processing run %s" % json.dumps(run))
    run_end_time = date_parser.parse(run["time_end"])
    run_age_seconds = (datetime.now(tz=timezone.utc) - run_end_time).seconds
    if run_age_seconds <= RUNS_MAX_AGE_SECONDS:
      recent_run_count += 1

  logging.info("Finished processing runs, recent runs=%d" % recent_run_count)
  return "%d" % recent_run_count

def main(argv):
  # This method is only used when running locally
  if len(argv) > 2:
    raise app.UsageError("Too many command-line arguments.")
  if "localhost" in argv:
    app.run(host='127.0.0.1', port=8080, debug=True)
    return
  print(get_num_recent_stable_runs())

if __name__ == '__main__':
  main(sys.argv[1:])
