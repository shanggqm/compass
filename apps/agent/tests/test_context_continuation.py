"""Continuation prefix changes at the common model compilation boundary."""

from hashlib import sha256

from compass_agent.context_runtime.contracts import PreparedContext
from compass_agent.context_runtime.model_step import reconcile_continuation
from compass_agent.llm.continuation import canonical, visible_messages
from compass_agent.schemas import ChatMessage, GenerateInput


def test_compilation_rebuilds_changed_prefix_and_keeps_security_transformation():
    draft = GenerateInput(
        messages=[
            ChatMessage(role="user", content="查询"),
            ChatMessage(role="assistant", content=""),
            ChatMessage(role="tool", content="结果", tool_call_id="q"),
        ],
        continuation_token="sealed",
    )
    continuation = {
        "visible_count": 2,
        "visible_digest": sha256(canonical(visible_messages(draft)[:2])).hexdigest(),
    }
    secured = draft.model_copy(
        update={
            "messages": [
                draft.messages[0],
                ChatMessage(
                    role="assistant",
                    content='<untrusted_context source="tool"></untrusted_context>',
                ),
                draft.messages[2],
            ]
        }
    )
    prepared = PreparedContext(secured, {})
    rebuilt, token = reconcile_continuation(draft, prepared, continuation)
    assert token is None and rebuilt.request.continuation_token is None
    assert rebuilt.request.messages == secured.messages
    assert rebuilt.receipt["continuationReset"] == "context_prefix_changed"
    # An invalid incoming token remains invalid; compilation cannot authorize it.
    broken = {**continuation, "visible_digest": "0" * 64}
    assert reconcile_continuation(draft, prepared, broken) == (prepared, broken)
    unchanged = PreparedContext(draft, {})
    assert reconcile_continuation(draft, unchanged, continuation) == (unchanged, continuation)


def test_new_tool_observation_does_not_invalidate_unchanged_prefix():
    draft = GenerateInput(
        messages=[
            ChatMessage(role="user", content="查询"),
            ChatMessage(role="tool", content="原件", tool_call_id="q"),
        ],
        continuation_token="sealed",
    )
    continuation = {
        "visible_count": 1,
        "visible_digest": sha256(canonical(visible_messages(draft)[:1])).hexdigest(),
    }
    prepared = PreparedContext(
        draft.model_copy(
            update={
                "messages": [
                    draft.messages[0],
                    ChatMessage(role="tool", content="安全包装后的原件", tool_call_id="q"),
                ]
            }
        ),
        {},
    )
    assert reconcile_continuation(draft, prepared, continuation) == (prepared, continuation)


def test_new_owner_confirmation_recompiles_context_once_and_replay_keeps_decision():
    from compass_agent.context_runtime.model_step import refresh_owner_context

    draft = GenerateInput(
        messages=[ChatMessage(role="user", content="原始问题")],
        continuation_token="sealed",
        thinking=True,
    )
    choice = [{"mention": "收入", "definition": "扣退款后入账额"}]
    # A newly approved choice must be compiled even when the provider has native state.
    rebuilt, signature = refresh_owner_context(draft, choice, {})
    assert rebuilt.continuation_token is None and rebuilt.thinking is False
    assert rebuilt.messages == draft.messages
    # Accepted frame receipts carry the version across worker changes. Subsequent
    # calls retain native continuation until the owner makes another decision.
    receipt = {"ownerDecisionDigest": signature}
    assert refresh_owner_context(draft, choice, dict(receipt)) == (draft, signature)
    changed, revised = refresh_owner_context(
        draft, [{"mention": "收入", "definition": "支付成功金额"}], receipt
    )
    assert changed.continuation_token is None and revised != signature
    assert refresh_owner_context(draft, [], {})[0] is draft
