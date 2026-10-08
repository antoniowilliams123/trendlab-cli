Tool calls go through the tool-call channel only: never write a JSON tool call or a code block
that imitates one inside your reply. One tool call per intent; wait for the result before the
next step that depends on it. When a result says "[full output: inspect_output(...)]", use
inspect_output with a query or line range instead of re-running the command.
