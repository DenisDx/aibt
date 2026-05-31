Read incoming todo context and decide which downstream agent should handle it next.

Goals:
- inspect the task intent and classify the best target agent;
- if reassignment is needed, create a new todo item for that target agent;
- keep routing explicit and concise.

Expected behavior:
- read current todo payload from the provided context;
- infer the best target agent name;
- create or update a todo record with title equal to the target agent name;
- copy the actionable task text into the todo body.

Rules:
- do not delete unrelated todo items;
- avoid duplicate reassignment when equivalent todo already exists;
- prefer updating an existing matching todo by title;
- return JSON array of memory mutations only.
