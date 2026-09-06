You are the adversary in a system that reverse-engineers APIs.

Another model observed an API, formed hypotheses about how it really behaves,
ran experiments, and recorded the beliefs below as CONFIRMED. It marked its
own homework. Your job is to find where it is wrong.

For each belief, ask specifically:

  1. Does the stated claim actually follow from the evidence cited, or does it
     assert a CAUSE that was never tested? "Capped at 50" follows from a
     sweep. "Capped at 50 because of your subscription tier" does not, unless
     tiers were varied.
  2. Is the claim over-general? A cap observed for one vendor is not a global
     cap until another vendor was tried.
  3. Is there a cheaper, more boring explanation that fits the same evidence?
  4. What single experiment would most likely BREAK this belief if it is
     wrong? Be concrete: which call, which parameters, which outcome would
     refute it.

BELIEFS UNDER REVIEW
  id: lab.create.the_api_enforces_a_hard_internal_limit_of_25
    class: silent_truncation  operation: create  parameter: title
    docs claim: stored as sent (doc: title_max=2000)
    belief    : The API silently truncates the 'title' field upon storage, returning the truncated version in the read-back.
    action    : Do not rely on the 'title' field containing the full input length; it will be truncated.
  id: lab.search.the_item_is_created_in_a_draft_state_and_is
    class: eventual_consistency  operation: search  parameter: None
    docs claim: items are immediately searchable
    belief    : The search query returned zero rows after a 0.5 second wait.
    action    : Ensure items are published before searching.
  id: lab.search.the_rate_limit_is_global_shared_across_all_u
    class: rate_limit  operation: search  parameter: None
    docs claim: no rate limit; 429 never returned
    belief    : 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
    action    : Retry with exponential backoff.
  id: lab.update.the_validation_logic_is_buggy_and_only_check
    class: silent_coercion  operation: update  parameter: not_a_real_field
    docs claim: 400 unknown_field
    belief    : The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.
    action    : Be cautious with required fields; assume the API might be lenient.

THE EVIDENCE EACH ONE RESTS ON
  lab.create.the_api_enforces_a_hard_internal_limit_of_25
    probe probe-000: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 3, "retry_after_absent": 4, "header_reliable": false}
      learned: The endpoint throttles requests, allowing at most 3 requests before throttling begins.
  lab.search.the_item_is_created_in_a_draft_state_and_is
    probe probe-009: template=timing calls=2
      facts: {"before": 0, "after": 0, "waited_s": 0.5, "changed": false, "delta": 0}
      learned: The search query returned zero rows after a 0.5 second wait.
    probe probe-009: template=timing calls=2
      facts: {"before": 0, "after": 0, "waited_s": 0.5, "changed": false, "delta": 0}
      learned: The search query returned zero rows after a 0.5 second wait.
    probe probe-009: template=timing calls=2
      facts: {"before": 0, "after": 0, "waited_s": 0.5, "changed": false, "delta": 0}
      learned: The search query returned zero rows after a 0.5 second wait.
    probe probe-009: template=timing calls=2
      facts: {"before": 0, "after": 0, "waited_s": 0.5, "changed": false, "delta": 0}
      learned: The search query returned zero rows after a 0.5 second wait.
    probe probe-009: template=timing calls=2
      facts: {"before": 0, "after": 0, "waited_s": 0.5, "changed": false, "delta": 0}
      learned: The search query returned zero rows after a 0.5 second wait.
    probe probe-009: template=timing calls=2
      facts: {"before": 0, "after": 0, "waited_s": 0.5, "changed": false, "delta": 0}
      learned: The search query returned zero rows after a 0.5 second wait.
  lab.search.the_rate_limit_is_global_shared_across_all_u
    probe probe-010: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 4, "retry_after_absent": 3, "header_reliable": false}
      learned: 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
    probe probe-010: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 4, "retry_after_absent": 3, "header_reliable": false}
      learned: 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
    probe probe-010: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 4, "retry_after_absent": 3, "header_reliable": false}
      learned: 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
    probe probe-010: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 4, "retry_after_absent": 3, "header_reliable": false}
      learned: 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
    probe probe-010: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 4, "retry_after_absent": 3, "header_reliable": false}
      learned: 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
    probe probe-010: template=header_burst calls=10
      facts: {"n": 10, "throttled": 7, "first_throttle_at": 4, "retry_after_present": 4, "retry_after_absent": 3, "header_reliable": false}
      learned: 7 out of 10 requests were throttled, with the first throttling occurring on the 4th request.
  lab.update.the_validation_logic_is_buggy_and_only_check
    probe probe-011: template=consistency calls=2
      facts: {"create_status": 200, "get_status": 200, "echo_matches_sent": false, "mismatches": {"not_a_real_field": {"sent": "test_value", "stored": null}}, "readback_matches_echo": true}
      learned: The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.
    probe probe-011: template=consistency calls=2
      facts: {"create_status": 200, "get_status": 200, "echo_matches_sent": false, "mismatches": {"not_a_real_field": {"sent": "test_value", "stored": null}}, "readback_matches_echo": true}
      learned: The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.
    probe probe-011: template=consistency calls=2
      facts: {"create_status": 200, "get_status": 200, "echo_matches_sent": false, "mismatches": {"not_a_real_field": {"sent": "test_value", "stored": null}}, "readback_matches_echo": true}
      learned: The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.
    probe probe-011: template=consistency calls=2
      facts: {"create_status": 200, "get_status": 200, "echo_matches_sent": false, "mismatches": {"not_a_real_field": {"sent": "test_value", "stored": null}}, "readback_matches_echo": true}
      learned: The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.
    probe probe-011: template=consistency calls=2
      facts: {"create_status": 200, "get_status": 200, "echo_matches_sent": false, "mismatches": {"not_a_real_field": {"sent": "test_value", "stored": null}}, "readback_matches_echo": true}
      learned: The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.
    probe probe-011: template=consistency calls=2
      facts: {"create_status": 200, "get_status": 200, "echo_matches_sent": false, "mismatches": {"not_a_real_field": {"sent": "test_value", "stored": null}}, "readback_matches_echo": true}
      learned: The field `not_a_real_field` was present in the readback and stored as null, despite being sent with the value 'test_value'.

Write your answer to `falsification.json` in the repository root as a single JSON
object, then stop. Do not modify any other file.

{
  "reviews": [
    {
      "belief_id": "<id>",
      "verdict": "sound" | "overgeneralised" | "unsupported_cause" | "wrong",
      "attack": "<the one experiment most likely to break it, concretely>",
      "because": "<one sentence, citing the specific evidence gap>"
    }
  ]
}

Be harsh. A belief you cannot fault is fine, but say why you could not fault
it rather than agreeing by default.