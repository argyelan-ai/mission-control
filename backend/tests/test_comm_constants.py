from app.comm_constants import (
    MESSAGE_TYPES, QUESTION_PRIORITIES, QUESTION_TARGETS,
    WAITING_TIMEOUT_SECONDS, SIDE_THREAD_ROUND_LIMIT, THREAD_KINDS,
)

def test_message_types_are_the_five_canonical():
    assert MESSAGE_TYPES == ("message", "question", "status", "decision", "system")

def test_question_priorities_match_task_priorities():
    assert QUESTION_PRIORITIES == ("low", "medium", "high", "critical")

def test_thread_kinds():
    assert THREAD_KINDS == ("task", "side", "dm")

def test_waiting_timeout_two_hours():
    assert WAITING_TIMEOUT_SECONDS == 7200

def test_round_limit():
    assert SIDE_THREAD_ROUND_LIMIT == 10

def test_question_targets():
    assert QUESTION_TARGETS == ("mark", "boss", "agent")
