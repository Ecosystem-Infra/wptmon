#!/usr/bin/env python3
# Lint as: python3
"""Placeholder main

This will monitor some wpt jobs eventually
"""
import logging
import sys

import flask

logging.basicConfig(level=logging.DEBUG)
app = flask.Flask(__name__)

@app.route("/")
def foo():
  logging.info("Running foo")
  return "All your WPT are monitor to us!"

def main(argv):
  # This method is only used when running locally
  if len(argv) > 2:
    raise app.UsageError("Too many command-line arguments.")
  if "localhost" in argv:
    app.run(host='127.0.0.1', port=8080, debug=True)
    return
  print(foo())

if __name__ == '__main__':
  main(sys.argv[1:])
