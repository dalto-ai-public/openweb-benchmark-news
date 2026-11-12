# Start here

Use Python 3.11 or later on a POSIX host. Install from this source tree or the
source archive, then run the synthetic example in a new directory.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/openweb-news example --output "$HOME/openweb-news-example"
```

The example writes a report and uses synthetic responses. It needs no accounts.
For the full path starting with one question per line, follow the
[installation guide](INSTALL.md#primary-first-run-path). You can run the commands
yourself or use a coding assistant.

For live use, keep credentials in an [external environment file](INSTALL.md#credentials-and-configuration)
and approve the exact prepared execution before dispatch. To understand existing
figures, start with [score replay](REPRODUCE.md).
