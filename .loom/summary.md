The brief header parser in bus/tickets.py now splits a header line that contains multiple packed headers (e.g., "Difficulty: medium Priority: P2 Model: gpt-6-luna") into individual headers before validation. The parser only splits when every part starts with a known header key (Difficulty, Priority, Foundational, Model, Base, Not before, After, Deliverable, Repairs incident). Unknown keys in the value region prevent splitting, preserving existing error behavior.

## Acceptance criteria
- [x] A new test module `tests/test_packed_brief_headers.py` parses a body whose first line is `Difficulty: medium Priority: P2 Model: gpt-6-luna`. It asserts that the headers read as Difficulty `medium`, Priority `P2` and Model `gpt-6-luna`, and it fails on main.
- [x] The same module asserts that `Difficulty: hardest` still raises the `Difficulty: must be one of easy, medium, hard` error.
- [x] A repeated header still reads as one header: `test_the_same_header_twice_is_one_header` in `tests/test_bus_tickets.py` still passes unchanged.

## Risk
Door: two-way
Blast radius: ticket

## Operator steps before merge
none

## Bugs discovered
none