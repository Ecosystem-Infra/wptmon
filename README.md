# wptmon
This is a prototype branch, lost of hackery.

The basic mantra of this effort is "No Surprises". More specifically, we want to
have confidence in our production systems. The primary pillars of this are
monitoring (keeping track of various metrics over time) and alerting (being
notified when metrics look wrong), which is mostly what's covered by this code
right now.

## Command reference

To deploy source code:

 * $ gcloud config set project wptmon
 * $ gcloud app deploy

Cron config needs to be deployed separately:

* $ gcloud app deploy cron.yaml

To check entire config:

* $ gcloud config list

To check project config:

* $ gcloud config get-value project

