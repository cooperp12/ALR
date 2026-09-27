# Contract summary

Input: user question, complete measurements with stable IDs, representative machine events.

Output: `selected_measurement_id`, `reason`, `confidence` only.

Python validates that the ID exists and is complete, reconstructs provenance and fields, creates the
measurement contract, derives the maximum/tie deterministically, and performs independent validation.
