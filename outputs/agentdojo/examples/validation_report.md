# AgentDojo multi-example graph validation

Validated 6/6 examples.

| Example | Existing task | Execution shape | Utility | Nodes | Edges | Result |
|---|---|---|---:|---:|---:|---|
| single_read | user_task_28 | one successful tool call | True | 5 | 4 | PASS |
| sequential_delete | user_task_35 | two sequential successful calls | True | 9 | 9 | PASS |
| sequential_write | user_task_29 | read followed by state mutation | True | 9 | 9 | PASS |
| parallel_then_write | user_task_20 | two parallel calls followed by one write | True | 12 | 14 | PASS |
| outgoing_email | user_task_33 | read followed by outgoing action | True | 9 | 9 | PASS |
| failed_then_recover | user_task_28 | invalid call followed by successful recovery | True | 8 | 8 | PASS |

The expectations are computed independently from the planned AgentDojo turns:
one LLM node per model call, one Tool node per FunctionCall, one SysOp for each
successful runtime execution, and one Data node per exact tool feedback
artifact. Every prior tool feedback message is expected to be input_to every
later LLM call because AgentDojo passes the full transcript to its standard LLM
elements. Failed pre-runtime calls produce a tool_error Data artifact from the
Tool node without fabricating a SysOp.

PASS requires more than matching totals: every edge must have the expected
endpoint types and high confidence; each Tool must follow exactly one invoking
LLM; each SysOp must follow a matching Tool action; each Data artifact must have
the correct producer kind; every artifact must connect to exactly the complete
set of later LLM calls; and the graph must remain acyclic and privacy-safe.

No domain read, write, payload_of, destination, or approval relation is expected
because AgentDojo 0.1.35 does not expose stable evidence for those relations at
the transcript boundary.

## Details

### single_read

- Tools: search_files_by_filename
- Expected nodes: {'llm_invocation': 2, 'tool_call': 1, 'system_operation': 1, 'data_object': 1}
- Actual nodes: {'llm_invocation': 2, 'tool_call': 1, 'system_operation': 1, 'data_object': 1}
- Expected edges: {'invokes': 1, 'triggers': 1, 'produces': 1, 'input_to': 1}
- Actual edges: {'invokes': 1, 'triggers': 1, 'produces': 1, 'input_to': 1}
- Issues: none

### sequential_delete

- Tools: list_files, delete_file
- Expected nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 2, 'data_object': 2}
- Actual nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 2, 'data_object': 2}
- Expected edges: {'invokes': 2, 'triggers': 2, 'produces': 2, 'input_to': 3}
- Actual edges: {'invokes': 2, 'triggers': 2, 'produces': 2, 'input_to': 3}
- Issues: none

### sequential_write

- Tools: search_files_by_filename, append_to_file
- Expected nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 2, 'data_object': 2}
- Actual nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 2, 'data_object': 2}
- Expected edges: {'invokes': 2, 'triggers': 2, 'produces': 2, 'input_to': 3}
- Actual edges: {'invokes': 2, 'triggers': 2, 'produces': 2, 'input_to': 3}
- Issues: none

### parallel_then_write

- Tools: get_day_calendar_events, search_contacts_by_name, create_calendar_event
- Expected nodes: {'llm_invocation': 3, 'tool_call': 3, 'system_operation': 3, 'data_object': 3}
- Actual nodes: {'llm_invocation': 3, 'tool_call': 3, 'system_operation': 3, 'data_object': 3}
- Expected edges: {'invokes': 3, 'triggers': 3, 'produces': 3, 'input_to': 5}
- Actual edges: {'invokes': 3, 'triggers': 3, 'produces': 3, 'input_to': 5}
- Issues: none

### outgoing_email

- Tools: search_files_by_filename, send_email
- Expected nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 2, 'data_object': 2}
- Actual nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 2, 'data_object': 2}
- Expected edges: {'invokes': 2, 'triggers': 2, 'produces': 2, 'input_to': 3}
- Actual edges: {'invokes': 2, 'triggers': 2, 'produces': 2, 'input_to': 3}
- Issues: none

### failed_then_recover

- Tools: causalguard_missing_tool, search_files_by_filename
- Expected nodes: {'llm_invocation': 3, 'tool_call': 2, 'system_operation': 1, 'data_object': 2}
- Actual nodes: {'llm_invocation': 3, 'tool_call': 2, 'data_object': 2, 'system_operation': 1}
- Expected edges: {'invokes': 2, 'triggers': 1, 'produces': 2, 'input_to': 3}
- Actual edges: {'invokes': 2, 'produces': 2, 'input_to': 3, 'triggers': 1}
- Issues: none
