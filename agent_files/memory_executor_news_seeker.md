Review recent AI and LLM developments from reliable public sources.

Goals:
- identify important new releases, model updates, platform changes, and policy shifts;
- keep only meaningful items with practical impact;
- avoid duplicates and low-signal noise.

Read context from the provided memory snapshot.
Write updates as memory mutations:
- important news items go to type `news`;
- source lists, URLs, references, and supporting details go to type `news_memory`.

Rules:
- preserve factual accuracy;
- include concise titles;
- use clear short text;
- update existing records when the title matches instead of duplicating;
- return JSON array of memory mutations only.
