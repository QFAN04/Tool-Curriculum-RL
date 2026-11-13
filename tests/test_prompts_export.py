"""提示渲染与导出：答题目标能被奖励判为满分；verl 奖励入口两种反馈设置；反向样本的结构。"""
import json
import os

from tcrl import verl_reward
from tcrl.data.export import rl_rows, sft_rows
from tcrl.prompts import generate_messages, render_calls, render_tools, solve_messages, solve_target
from tcrl.reward import compute_score

TOOLS = [{"name": "get_weather", "description": "d", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
CALL = {"id": "a", "kind": "call", "question": "Weather in Paris?", "tools": TOOLS,
        "gold": [{"name": "get_weather", "arguments": {"city": "Paris"}}], "refusal": None}
REF = {"id": "b", "kind": "refuse", "question": "Tell me a joke", "tools": TOOLS, "gold": [], "refusal": "Sure, here is one."}


def test_sft_target_scores_one_under_reward():
    for r in (CALL, REF):
        assert compute_score(solve_target(r), r["gold"], r["tools"])["score"] == 1.0


def test_tools_rendering_is_deterministic_and_in_system_message():
    a = dict(TOOLS[0]); b = {k: a[k] for k in reversed(list(a))}
    assert render_tools([a]) == render_tools([b])
    msgs = solve_messages(CALL)
    assert [m["role"] for m in msgs] == ["system", "user"] and "get_weather" in msgs[0]["content"]
    assert msgs[1]["content"] == "Weather in Paris?"


def test_generate_messages_contain_calls_not_question():
    msgs = generate_messages(TOOLS, CALL["gold"])
    assert render_calls(CALL["gold"]) in msgs[1]["content"] and "Weather in Paris?" not in json.dumps(msgs)


def test_sft_rows_have_reverse_samples_only_from_call_records():
    rows = sft_rows([CALL, REF] * 50, reverse_frac=0.1)
    gen = [r for r in rows if r["task"] == "generate"]
    assert len(gen) == 10 and all(r["messages"][-1]["content"] == "Weather in Paris?" for r in gen)
    assert all(r["messages"][-1]["role"] == "assistant" for r in rows)


def test_rl_rows_and_verl_reward_entry(monkeypatch):
    row = rl_rows([CALL], "rl")[0]
    assert row["data_source"] == "tcrl" and row["prompt"] == solve_messages(CALL)
    good = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
    bad = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Rome"}}\n</tool_call>'
    gt, ei = row["reward_model"]["ground_truth"], row["extra_info"]
    assert verl_reward.compute_score("tcrl", good, gt, ei)["score"] == 1.0
    monkeypatch.setenv("TCRL_FEEDBACK_GOLD", "1")
    assert "Paris" in verl_reward.compute_score("tcrl", bad, gt, ei)["feedback"]
    monkeypatch.setenv("TCRL_FEEDBACK_GOLD", "0")
    fb = verl_reward.compute_score("tcrl", bad, gt, ei)["feedback"]
    assert "Paris" not in fb and fb


def test_sft_special_token_init_and_grad_routing():
    import types
    import torch
    import torch.nn as nn
    from tcrl.sft.train_sft import (
        SPECIAL_TOKEN_INIT,
        enable_special_token_grads,
        init_special_embeddings,
        sync_special_rows,
    )

    specials = list(SPECIAL_TOKEN_INIT)
    vocab_tokens = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"] + specials
    tok_to_id = {t: idx for idx, t in enumerate(vocab_tokens)}

    class DummyTok:
        def convert_tokens_to_ids(self, s):
            return tok_to_id[s]

        def encode(self, text, add_special_tokens=False):
            # Deterministic non-identical subword ids from characters in text
            return [sum(ord(ch) for ch in text[i : i + 2]) % 10 for i in range(0, len(text), 2)]

    class DummyModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.config = types.SimpleNamespace(tie_word_embeddings=True)
            self.emb = nn.Embedding(len(vocab_tokens), 8)
            with torch.no_grad():
                torch.manual_seed(0)
                self.emb.weight[:10].normal_(0, 1.0)
                # Collapse all special rows onto the exact same vector (mimicking Qwen3-4B-Base)
                collapsed = torch.ones(8) * 0.1
                for tid in range(10, len(vocab_tokens)):
                    self.emb.weight[tid] = collapsed

        def get_input_embeddings(self):
            return self.emb

        def forward(self, input_ids):
            h = self.emb(input_ids)
            return h @ self.emb.weight.T

    model = DummyModel()
    tok = DummyTok()
    special_ids = init_special_embeddings(model, tok)
    assert special_ids == list(range(10, 10 + len(specials)))

    # Special rows should now be distinct and rescaled to median normal norm
    w = model.get_input_embeddings().weight
    normal_median = w[:10].norm(dim=1).median().item()
    for tid in special_ids:
        assert abs(w[tid].norm().item() - normal_median) < 1e-4
    sp_vecs = w[special_ids]
    assert not torch.allclose(sp_vecs[0], sp_vecs[1])

    # Calling init again on an already-initialized/trained model (e.g., E0+) must preserve weights
    sp_snapshot = sp_vecs.detach().clone()
    init_special_embeddings(model, tok)
    assert torch.equal(w[special_ids], sp_snapshot)

    # Freeze normal rows; only special_ids should update after step + sync
    normal_before = w[:10].detach().clone()
    sp_before = w[special_ids].detach().clone()
    sp_param = enable_special_token_grads(model, special_ids)
    opt = torch.optim.SGD([sp_param], lr=0.5)

    x = torch.tensor([[0, 1, special_ids[0], special_ids[2]]])
    logits = model(x)
    loss = logits[:, :, special_ids[0]].sum() - logits[:, :, 0].sum()
    loss.backward()
    w.grad = None
    opt.step()
    sync_special_rows(model, special_ids, sp_param)

    assert torch.equal(w[:10], normal_before)
    assert not torch.equal(w[special_ids], sp_before)
