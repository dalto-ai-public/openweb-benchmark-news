# Reproduce the saved scores

Use the separate saved benchmark dataset for the 400-question, 13-configuration
run. It includes exact questions and contracts, original result ranks, saved
grades and unresolved states, and the expected per-query and aggregate scores.
The dataset manifest identifies the scoring code and method assets it needs.

For exact replay, use the tested Python 3.14.7 and install the matching wheel.
Then run the verifier from the dataset directory.

```sh
python3.14 -m venv .replay-venv
.replay-venv/bin/python -m pip install --no-index --no-deps /path/to/benchmark.whl
.replay-venv/bin/python -B -I replay.py
```

The verifier reads saved data and recalculates metrics offline. Python 3.11 uses
a different float summation algorithm and produced last-place differences in
the replay check. The regular runner supports Python 3.11 and later.

Slot summaries cover all 400 questions. The exact family-weighted nDCG@10
values cover 53 resolved questions and 53 families. Keep these scopes with the
figures. Fresh collection uses the reader's provider accounts and the current
search index. Fresh judging can also produce different grades.

CC BY 4.0 applies to the benchmark material identified as licensed by Dalto in
the dataset notice. That notice identifies any included third-party content and
applicable attribution. [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
