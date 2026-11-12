# OpenWeb Benchmark — News

Start with [START.md](START.md) and [INSTALL.md](INSTALL.md). The packaged
method assets define scoring and judging. Frozen packages and saved receipts
bind the settings and evidence for each run.

Use synthetic fixtures for development. Keep real credentials, raw provider
responses, model responses, execution receipts and run outputs outside the
source tree. Live execution requires the operator's approval of the exact
package and spending scope. Preserve completed and failed attempts on resume.

Run the standard-library tests with `python -m unittest discover -v`. Keep
changes focused, document provider-specific options and verify the installed
workflow after packaging changes.
