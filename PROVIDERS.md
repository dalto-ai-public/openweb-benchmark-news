# Provider settings for the accepted news benchmark

The 400-question run tested these 13 configurations. The request settings below come from the selected successful attempts. The benchmark judged the returned title, relevant metadata and full snippets in its selected evidence fields. This reflects the customer workflow of reading search responses first and opening links occasionally. Fetching and judging every linked page would add substantially more work.

The selection rule was to stay as close as practical to provider defaults while choosing evidence that made sense for judging and customer use. We used general or web search across the roster and kept snippets manageable through request options where appropriate. The table lists the settings actually sent. A setting being explicit in the request does not establish whether it was a historical provider default.

| Configuration | Product and evidence settings sent | Structured date control |
| --- | --- | --- |
| Exa Highlights | `type=auto`, `contents.highlights=true`, `numResults=10` | `startPublishedDate` and `endPublishedDate` |
| Perplexity Web | `search_type=web`, `max_results=10`. The request omits `search_context_size`. | `search_after_date_filter` and `search_before_date_filter` |
| Brave Web | Web search, `count=10`, `extra_snippets=true`, `text_decorations=false` | Effective `freshness` range, with a saved first-dispatch anchor |
| Firecrawl Web | `sources=["web"]`, `highlights=false`, `limit=10` | None |
| Searlo Web | Web search, `limit=10` | None |
| Parallel Advanced | `mode=advanced`. The benchmark evaluates up to the first ten returned positions. | `advanced_settings.source_policy.after_date` lower bound |
| Tavily General | `topic=general`, `search_depth=basic`, `max_results=10`, `auto_parameters=false`, `include_answer=false`, `include_raw_content=false` | `start_date` lower bound |
| Linkup Fast | `depth=fast`, `outputType=searchResults`, `maxResults=10` | `fromDate` and `toDate`. The saved method documents the limit of the native date meaning. |
| You.com Web | Web search, `count=10` | Effective `freshness` range, with a saved first-dispatch anchor |
| Valyu Search | `search_type=web`, `country_code=ALL`, `max_num_results=10` | `start_date` and `end_date` |
| Oxylabs Search | Instant search, `limit=10` | None |
| Keenable Pro | `mode=pro`, `max_results=10` | `published_after` and `published_before` |
| Seltz Web Pro | `scope=web`, `tier=pro`, `max_results=10`, `fields.content=false`, `fields.snippets=true` | `from_date` and `to_date` |

Each configuration used its listed stable settings for all 400 accepted searches. Each of the ten configurations with a structured date control sent it for 395 questions. The other five questions had no such control. Supported date meanings and bounds differ by provider. Retrieval filters guide the search, and publication eligibility is judged from the returned evidence under the benchmark method.

Recorded choices include using Tavily General for the common web-search scope, keeping Brave's extra snippets, and omitting Perplexity's explicit context-size setting after the pre-run diagnostic. The recorded Firecrawl choice disabled highlights. Seltz requested snippet objects with full-document content disabled. Keenable used the existing Pro route and native snippet length. For other individual mode and tier choices, the table records the selected settings. Specific contemporaneous rationales and historical default status remain to be cited where the source supports them.

The accepted captures span November 5–9, 2026 UTC. Brave's later recovery supplied 300 cells. The verified request audit counts accepted cells and excludes superseded attempts. It follows the nine frozen packages and matches the declared request, saved result and accepted collection status for each of 5,200 cells. It also verifies 395 Brave and 395 You.com effective-date receipts.


## Change the provider roster

Use a new configuration and a fresh run directory for each roster change. A
configuration ID identifies one endpoint and set of options. Keep the historical
configuration ID for replay if you are introducing another product mode.

To add or remove a supported configuration:

1. Start with the synthetic settings from `openweb-news starter --simple`.
   Update `provider_ids` and give every selected ID exactly one entry in
   `tie_order`.
2. Reconcile the provider credential references and the account entries in
   `execution_policy.scheduling.accounts`. If recovery is configured, reconcile
   `execution_policy.scheduling.recovery` with the same account set. An account
   shared by another selected configuration still belongs in the schedule.
3. Review `provider_date_profiles`, `provider_projection_profiles` and
   `provider_request_profiles` for the resulting roster. Prepare fresh date
   capabilities and date trust. Make explicit eligible-field choices for the
   new roster and accept a new exact method package.
4. Run the [synthetic first-run path](INSTALL.md#primary-first-run-path) with
   those settings. For live execution, prepare and approve its own exact
   package using the external credentials selected for the new roster.

The offline example in `tests.test_question_workflow` replaces Brave with
Valyu, updates tie order, accounts and date trust, and checks a complete
synthetic report and resume. From the source archive or repository run:

```sh
python -m unittest tests.test_question_workflow.QuestionWorkflowTests.test_plain_text_to_closed_report_via_public_cli_and_no_send_resume
```

For a new adapter, add its reviewed catalog entry in
[providers.json](openweb_news/assets/providers.json). Verify the exact request,
credential reference and native response projection in
[the provider adapter](openweb_news/_method/providers.py). Review the matching
date capability and field role in
[provider_dates.py](openweb_news/provider_dates.py) and
[date_trust.py](openweb_news/date_trust.py). Pin success, empty, malformed and
failure shapes with synthetic fixtures before using the roster procedure above.
Document its product mode, date semantics, evidence controls and cost estimates
separately from any account-specific live access.
