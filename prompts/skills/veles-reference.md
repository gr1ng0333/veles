# Veles memory and diagnostics

- For prior decisions, use `chat_history` with a search or offset, or
  `memory_search` across chat, knowledge, summaries and task reflections. Check
  a claim about who said something against the original chat. Old dialogue
  summaries with unverified authorship are useful as leads.
- For recipes, use `knowledge_read` with the topic name. Find topics with
  `knowledge_list` or the `index-full` topic. The `patterns` topic records
  recurring failures. Use the relevant entry when working in its domain.
- For a task failure or recovery, discover `log_query` and filter by task ID
  and time. Select events, tool calls, progress, supervisor logs or reflections
  according to the question. Start with a small result limit.
- For prompt size, discover `context_inspect`. It reports the shared startup
  blocks by section. Check provider usage for measured tokens.
- For a source that needs exact wording, read the relevant pages with
  `drive_read` or `repo_read`. Continue with the reported offset and revision.
