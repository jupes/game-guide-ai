"""Offline tests for the decision-benchmark arms and CLI (agent-forge-harness-cps).

No test here reaches the network: OPENAI_API_KEY and TYPESAFE_API_KEY are removed from the
environment for every test, socket connections fail loudly, and the live code paths run
against fake clients.
"""

from __future__ import annotations

import json
import math
import socket
from types import SimpleNamespace

import pytest

from ingestion import decision_bench as db

FAKE_OPENAI_KEY = "fake-openai-key-for-tests-4f1d"
FAKE_TYPESAFE_KEY = "fake-typesafe-key-for-tests-9c2e"


@pytest.fixture(autouse=True)
def no_keys_no_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _item(i: int, label: str, group: str | None = None, mode: str = "gm", answer: str | None = None,
          subset: str = "core", category: str = "test") -> db.Item:
    return db.Item(id=f"t-{i}", mode=mode, query=f"q{i}", answer=answer or f"answer {i}", label=label,
                   subset=subset, category=category, group=group or f"g{i}")


# ── heuristic arm ───────────────────────────────────────────────────────────────────────────

STAT_TEXT = "**Armor Class** 14\n**Hit Points** 45 (7d8 + 14)"


@pytest.mark.parametrize(("mode", "answer", "expected"), [
    ("spell", "Anything at all, even a refusal.", "spell_card"),
    ("sage", STAT_TEXT, "stat_block"),
    ("gm", STAT_TEXT, "stat_block"),
    ("gm", "The tavern is quiet tonight.", "none"),
    ("rules", STAT_TEXT, "none"),  # rules mode never structures (service/graph.py generate_route)
])
def test_heuristic_mirrors_the_service_rule(mode, answer, expected):
    assert db.heuristic_label(mode, answer) == expected


def test_heuristic_arm_is_certain_and_free():
    result = db.run_heuristic([_item(0, "stat_block", answer=STAT_TEXT)])
    d = result.decisions[0]
    assert (d.label, d.confidence, d.input_tokens) == ("stat_block", 1.0, 0)


# ── grouped folds and the embedding arm ─────────────────────────────────────────────────────


def test_grouped_folds_keep_each_group_in_one_fold_and_cover_every_label():
    groups = [f"g{i // 3}" for i in range(60)]  # 20 groups of 3
    labels = [("stat_block", "spell_card", "none")[(i // 3) % 3] for i in range(60)]
    folds = db.grouped_folds(groups, labels, k=5, seed=1)
    fold_of_group: dict[str, set[int]] = {}
    for g, f in zip(groups, folds, strict=True):
        fold_of_group.setdefault(g, set()).add(f)
    assert all(len(fs) == 1 for fs in fold_of_group.values())
    for f in range(5):
        assert {lab for lab, fo in zip(labels, folds, strict=True) if fo == f} == set(db.LABELS)
    assert db.grouped_folds(groups, labels, k=5, seed=1) == folds  # deterministic


def test_grouped_folds_reject_a_single_fold():
    with pytest.raises(ValueError):
        db.grouped_folds(["g"], ["none"], k=1, seed=0)


def test_nearest_centroid_probabilities():
    clf = db.NearestCentroid(temperature=0.1).fit(
        [[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0], [0, 0.9, 0.1], [0, 0, 1]],
        ["stat_block", "stat_block", "spell_card", "spell_card", "none"],
    )
    probs = clf.predict_proba([1, 0.05, 0])
    assert max(probs, key=probs.get) == "stat_block"
    assert sum(probs.values()) == pytest.approx(1.0)
    # softmax(cos / T): the stat_block centroid is ~[0.994, 0.110, 0] → sim ≈ 0.998 vs spell ≈ 0.049
    assert probs["stat_block"] > 0.99


def test_temperature_is_chosen_from_the_grid_by_held_out_likelihood():
    clf = db.NearestCentroid().fit([[1, 0], [0, 1]], ["stat_block", "none"])
    # Four held-out points at [0.8, 0.6], three labelled stat_block: the best p(stat_block) is
    # 0.75, i.e. 0.2 / T = ln 3, T ≈ 0.18. On the grid, NLL(0.15) = 2.269, NLL(0.2) = 2.253,
    # NLL(0.3) = 2.324, so 0.2 wins.
    t = clf.fit_temperature([[0.8, 0.6]] * 4, ["stat_block", "stat_block", "stat_block", "none"])
    assert t == 0.2
    assert clf.temperature == 0.2


def _clustered_items(n_groups: int = 15):
    """Three well-separated clusters, 2 items per group."""
    items, emb = [], {}
    axes = {"stat_block": [1.0, 0.0, 0.0], "spell_card": [0.0, 1.0, 0.0], "none": [0.0, 0.0, 1.0]}
    for g in range(n_groups):
        label = db.LABELS[g % 3]
        for j in range(2):
            it = _item(g * 2 + j, label, group=f"grp{g}")
            items.append(it)
            vec = [x + 0.01 * ((g + j) % 5) for x in axes[label]]
            emb[it.id] = db.Embedding(vec, tokens=100, latency_ms=200.0 + g)
    return items, emb


def test_embedding_arm_fold_log_scores_every_group_once():
    # The fold log is built from the train/test lists, so it cannot show what the classifier
    # was fitted on; test_embedding_arm_never_fits_or_tunes_on_a_group_it_scores does that.
    items, emb = _clustered_items()
    result = db.run_embedding(items, emb, k=5, seed=3)
    assert result.status == "ok" and len(result.decisions) == len(items)
    tested: list[str] = []
    for fold in result.extra["fold_log"]:
        assert not set(fold["train_groups"]) & set(fold["test_groups"])
        tested += fold["test_groups"]
    assert sorted(tested) == sorted({it.group for it in items})  # every group scored exactly once
    assert all(d.label == it.label for d, it in zip(result.decisions, items, strict=True))
    assert result.decisions[0].input_tokens == 100
    assert result.decisions[0].latency_ms >= 200.0  # recorded embedding latency + classifier time


def test_embedding_arm_never_fits_or_tunes_on_a_group_it_scores(monkeypatch):
    """Checks what each classifier was given, not what the fold log says. For every
    NearestCentroid that run_embedding builds, no group it scores was among the vectors
    its centroids were fitted on or its temperature was tuned on. Its temperature is also
    tuned on groups the centroids fitted just before had not seen."""
    items, base = _clustered_items()
    # A fourth coordinate unique to each item, so that a vector identifies its item.
    emb = {it.id: db.Embedding([*base[it.id].vector, 0.001 * (i + 1)], base[it.id].tokens, base[it.id].latency_ms)
           for i, it in enumerate(items)}
    item_of = {tuple(emb[it.id].vector): it for it in items}
    log: dict[db.NearestCentroid, list[tuple[str, set[str]]]] = {}
    tuning = [False]
    real_fit = db.NearestCentroid.fit
    real_tune = db.NearestCentroid.fit_temperature
    real_predict = db.NearestCentroid.predict_proba

    def groups(vectors):
        return {item_of[tuple(v)].group for v in vectors}

    def fit(self, vectors, labels):
        log.setdefault(self, []).append(("fit", groups(vectors)))
        return real_fit(self, vectors, labels)

    def fit_temperature(self, vectors, labels):
        log.setdefault(self, []).append(("tune", groups(vectors)))
        tuning[0] = True  # the grid search calls predict_proba; that is tuning, not scoring
        try:
            return real_tune(self, vectors, labels)
        finally:
            tuning[0] = False

    def predict_proba(self, vector):
        if not tuning[0]:
            log.setdefault(self, []).append(("score", groups([vector])))
        return real_predict(self, vector)

    monkeypatch.setattr(db.NearestCentroid, "fit", fit)
    monkeypatch.setattr(db.NearestCentroid, "fit_temperature", fit_temperature)
    monkeypatch.setattr(db.NearestCentroid, "predict_proba", predict_proba)

    result = db.run_embedding(items, emb, k=5, seed=3)

    scored: list[str] = []
    tuned = 0
    for events in log.values():
        seen: set[str] = set()  # every group this classifier's centroids or temperature used
        last_fit: set[str] = set()
        for kind, gs in events:
            if kind == "score":
                assert not gs & seen, f"scored {gs} with a classifier fitted or tuned on it"
                scored += gs
            elif kind == "tune":
                assert not gs & last_fit, f"temperature tuned on {gs & last_fit}, which the centroids had seen"
                tuned += 1
                seen |= gs
            else:
                last_fit = gs
                seen |= gs
    assert sorted(scored) == sorted(it.group for it in items)  # every item scored exactly once
    assert tuned == 5  # the temperature really was tuned in every fold, so the check above ran
    assert len(result.decisions) == len(items)


def test_run_embedding_rejects_an_unknown_group_field():
    with pytest.raises(ValueError):
        db.run_embedding([], {}, group_field="template")


def test_run_embedding_grouped_by_category_keeps_a_template_family_in_one_fold():
    """Every item here has its OWN `group` (so grouping by `group` would happily split a
    template family across folds), but items share a `category` in pairs. Asking for
    group_field="category" must still keep each pair on one side of every fold."""
    items, emb = [], {}
    axes = {"stat_block": [1.0, 0.0, 0.0], "spell_card": [0.0, 1.0, 0.0], "none": [0.0, 0.0, 1.0]}
    for g in range(15):
        label = db.LABELS[g % 3]
        for j in range(2):
            it = _item(g * 2 + j, label, group=f"solo{g}-{j}", category=f"cat{g}")
            items.append(it)
            vec = [x + 0.01 * ((g + j) % 5) for x in axes[label]]
            emb[it.id] = db.Embedding(vec, tokens=100, latency_ms=200.0 + g)
    result = db.run_embedding(items, emb, k=5, seed=3, group_field="category")
    assert result.extra["group_field"] == "category"
    tested: list[str] = []
    for fold in result.extra["fold_log"]:
        assert not set(fold["train_groups"]) & set(fold["test_groups"])
        tested += fold["test_groups"]
    # Every category scored exactly once: had the code fallen back to per-instance `group`
    # fields (each item's own, unique group), this would instead list 30 one-item groups.
    assert sorted(tested) == sorted({it.category for it in items})


def _fake_openai_embeddings(dim: int = 4):
    calls: list[list[str]] = []

    def create(model, input):  # noqa: A002 - mirrors the OpenAI SDK signature
        calls.append(input)
        vec = [float(len(input[0]) % 7), 1.0, 0.5, 0.25][:dim]
        return SimpleNamespace(data=[SimpleNamespace(embedding=vec)], usage=SimpleNamespace(prompt_tokens=42))

    return SimpleNamespace(embeddings=SimpleNamespace(create=create)), calls


def test_embeddings_are_recorded_live_then_replayed_without_a_client(tmp_path):
    items = [_item(0, "none"), _item(1, "stat_block")]
    cache = tmp_path / "emb.jsonl"
    client, calls = _fake_openai_embeddings()
    first = db.get_embeddings(items, cache, client)
    assert len(calls) == 2 and first["t-0"].tokens == 42
    replayed = db.get_embeddings(items, cache, None)  # no client: must come from the recording
    assert replayed == first


def test_embeddings_without_key_or_recording_are_missing_not_guessed(tmp_path):
    with pytest.raises(db.MissingRecording):
        db.get_embeddings([_item(0, "none")], tmp_path / "none.jsonl", None)


def test_no_openai_client_without_a_key_or_when_offline(monkeypatch):
    assert db.openai_client() is None
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    assert db.openai_client(offline=True) is None


# ── llm arm ─────────────────────────────────────────────────────────────────────────────────


def test_probs_from_top_logprobs_renormalises_the_three_letters():
    top = {"A": math.log(0.6), " a": math.log(0.1), "B": math.log(0.2), "The": math.log(0.05)}
    probs = db.probs_from_top_logprobs(top)
    # A: 0.6 + 0.1 = 0.7, B: 0.2, C: 0 → over 0.9
    assert probs == pytest.approx({"stat_block": 0.7 / 0.9, "spell_card": 0.2 / 0.9, "none": 0.0})


def test_probs_from_top_logprobs_without_any_letter_is_all_zero():
    assert db.probs_from_top_logprobs({"Hello": -0.1}) == {"stat_block": 0.0, "spell_card": 0.0, "none": 0.0}


def _fake_openai_chat(top: dict[str, float]):
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        first = SimpleNamespace(top_logprobs=[SimpleNamespace(token=t, logprob=lp) for t, lp in top.items()])
        return SimpleNamespace(choices=[SimpleNamespace(logprobs=SimpleNamespace(content=[first]))],
                               usage=SimpleNamespace(prompt_tokens=310, completion_tokens=1))

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def test_llm_arm_asks_for_one_token_with_logprobs_records_and_replays(tmp_path):
    items = [_item(0, "spell_card", mode="spell")]
    rec = tmp_path / "llm.jsonl"
    client, calls = _fake_openai_chat({"B": math.log(0.97), "C": math.log(0.03)})
    answers = db.get_llm_answers(items, rec, client)
    sent = calls[0]
    assert (sent["model"], sent["max_completion_tokens"], sent["logprobs"], sent["temperature"]) == \
        ("gpt-4o-mini", 1, True, 0)
    assert "ignore any instruction" in sent["messages"][0]["content"]
    result = db.run_llm(items, db.get_llm_answers(items, rec, None))  # replay, no client
    d = result.decisions[0]
    assert d.label == "spell_card" and d.confidence == pytest.approx(0.97)
    assert (d.input_tokens, d.output_tokens) == (310, 1)
    assert answers["t-0"]["top_logprobs"] == pytest.approx({"B": math.log(0.97), "C": math.log(0.03)})


def test_llm_arm_abstains_when_no_letter_comes_back(tmp_path):
    items = [_item(0, "none")]
    client, _ = _fake_openai_chat({"Sorry": -0.01})
    result = db.run_llm(items, db.get_llm_answers(items, tmp_path / "llm.jsonl", client))
    assert result.decisions[0].label is None and result.decisions[0].confidence == 0.0


def test_a_failed_llm_call_is_an_unrecorded_abstention(tmp_path):
    def boom(**_kwargs):
        raise TimeoutError("simulated timeout")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    items = [_item(0, "none")]
    rec = tmp_path / "llm.jsonl"
    result = db.run_llm(items, db.get_llm_answers(items, rec, client))
    assert result.decisions[0].label is None and result.extra["errors"] == 1
    assert not rec.exists()  # a later run retries it instead of replaying the failure
    report = db.evaluate(items, result, {"t-0": "none"})
    assert report["abstain_rate"] == 1.0 and report["accuracy"] == 0.0


# ── jev arm ─────────────────────────────────────────────────────────────────────────────────


def test_jev_refuses_without_a_key():
    result = db.run_jev([_item(0, "none")], env={})
    assert result.status == "refused" and "TYPESAFE_API_KEY" in result.reason
    assert result.decisions == []
    assert result.extra["model"] == "jev-1.13.0"


def test_jev_with_a_key_is_still_a_stub_that_sends_nothing():
    # The autouse fixture makes any socket connection fail the test.
    result = db.run_jev([_item(0, "none")], env={"TYPESAFE_API_KEY": FAKE_TYPESAFE_KEY})
    assert result.status == "stub" and result.decisions == []
    assert FAKE_TYPESAFE_KEY not in json.dumps(result.extra) + result.reason


def test_jev_projects_cost_from_estimated_tokens():
    items = [_item(0, "none", answer="x" * 4000)]
    result = db.run_jev(items, env={})
    tokens = len(db.LLM_SYSTEM + db.embed_input(items[0])) // 4
    assert result.extra["projected_cost_per_1000_usd"] == pytest.approx(tokens * 0.042 / 1e6 * 1000)


# ── evaluate + CLI ──────────────────────────────────────────────────────────────────────────


def test_evaluate_reports_every_headline_metric():
    items = [_item(0, "stat_block", answer=STAT_TEXT), _item(1, "none")]
    heuristic = db.run_heuristic(items)
    report = db.evaluate(items, heuristic, {d.item_id: d.label for d in heuristic.decisions})
    for key in ("macro_f1", "ece_10_bins", "coverage", "none_veto_on_heuristic_positives", "adversarial",
                "latency_ms", "cost_per_1000_usd", "downstream_per_1000", "per_class", "confusion"):
        assert key in report
    assert report["macro_f1"] == pytest.approx((1.0 + 0.0 + 1.0) / 3)  # spell_card has no support
    assert set(report["coverage"]) == {"0.90", "0.95", "0.99"}


def test_evaluate_scores_only_the_adversarial_subset_as_adversarial():
    items = [_item(0, "none", subset="adversarial"), _item(1, "none", subset="adversarial"), _item(2, "none")]
    card = {"stat_block": 0.995, "spell_card": 0.004, "none": 0.001}
    decisions = [
        db.Decision("t-0", "none", {"none": 1.0}, 1.0),  # adversarial, held: says none
        db.Decision("t-1", "stat_block", card, 1.0),  # adversarial, not held: a card at 0.995
        db.Decision("t-2", "stat_block", card, 1.0),  # core: must not count as adversarial
    ]
    report = db.evaluate(items, db.ArmResult("embedding", "ok", decisions=decisions), {it.id: "none" for it in items})
    # Adversarial items 0 and 1: one held at every threshold (0.995 clears 0.99) → 1/2.
    # Were the core item scored instead, n would be 1 and the share 0.
    assert report["adversarial"] == pytest.approx(
        {"n": 2, "held_to_none_at_0.90": 0.5, "held_to_none_at_0.95": 0.5, "held_to_none_at_0.99": 0.5})


def test_evaluate_template_grouped_score_comes_from_the_template_run_not_the_committed_one():
    """Pins agent-forge-harness-69h: the report's `_template_grouped` sections must come from
    the `template` ArmResult's own decisions. A regression that silently reused the committed
    (per-instance) run's decisions instead would make every assertion below fail, because the
    two runs are built here to disagree on every item."""
    items = [
        _item(0, "none", subset="adversarial", category="prose_ac_hp"),
        _item(1, "none", subset="adversarial", category="prose_ac_hp"),
        _item(2, "stat_block", subset="hard_positive", category="abbreviated_block"),
    ]
    committed = db.ArmResult("embedding", "ok", decisions=[
        db.Decision("t-0", "none", {"none": 1.0}, 1.0),  # held
        db.Decision("t-1", "none", {"none": 1.0}, 1.0),  # held
        db.Decision("t-2", "stat_block", {"stat_block": 1.0}, 1.0),  # correct
    ], extra={"group_field": "group"})
    template = db.ArmResult("embedding", "ok", decisions=[
        db.Decision("t-0", "stat_block", {"stat_block": 0.995}, 1.0),  # category held out: not held
        db.Decision("t-1", "none", {"none": 1.0}, 1.0),  # still held
        db.Decision("t-2", "none", {"none": 0.995}, 1.0),  # category held out: wrong
    ], extra={"group_field": "category"})
    report = db.evaluate(items, committed, {it.id: "none" for it in items}, template=template)
    assert report["adversarial"]["held_to_none_at_0.99"] == pytest.approx(1.0)
    assert report["hard_positive"]["accuracy"] == pytest.approx(1.0)
    assert report["adversarial_template_grouped"] == pytest.approx(
        {"n": 2, "group_field": "category", "held_to_none_at_0.90": 0.5,
         "held_to_none_at_0.95": 0.5, "held_to_none_at_0.99": 0.5})
    assert report["hard_positive_template_grouped"] == pytest.approx(
        {"n": 1, "group_field": "category", "accuracy": 0.0})


def test_evaluate_omits_template_grouped_sections_without_a_template_run():
    items = [_item(0, "none", subset="adversarial")]
    result = db.ArmResult("embedding", "ok", decisions=[db.Decision("t-0", "none", {"none": 1.0}, 1.0)])
    report = db.evaluate(items, result, {"t-0": "none"})
    assert "adversarial_template_grouped" not in report and "hard_positive_template_grouped" not in report


def test_run_wires_a_category_grouped_pass_for_the_embedding_arm(tmp_path):
    items = [
        _item(0, "none", subset="adversarial", category="adv_a"),
        _item(1, "none", subset="adversarial", category="adv_a"),
        _item(2, "none", subset="adversarial", category="adv_b"),
        _item(3, "stat_block", subset="hard_positive", category="hp_a"),
        _item(4, "stat_block", subset="hard_positive", category="hp_a"),
        _item(5, "spell_card"), _item(6, "stat_block"), _item(7, "none"),
    ]
    embed, _ = _fake_openai_embeddings()
    client = SimpleNamespace(embeddings=embed.embeddings)
    [report] = db.run(["embedding"], items, tmp_path, offline=False, folds=3, seed=1,
                      client_factory=lambda _offline: client, env={})
    assert report["group_field"] == "group"  # the committed, per-instance run
    assert report["adversarial_template_grouped"]["group_field"] == "category"
    assert report["hard_positive_template_grouped"]["group_field"] == "category"
    assert report["adversarial_template_grouped"]["n"] == 3
    assert report["hard_positive_template_grouped"]["n"] == 2


def test_run_prices_each_arm_by_its_own_tokens(tmp_path):
    # Distinct categories: db.run() now also groups the embedding arm by category (agent-forge-
    # harness-69h), and every item sharing one category (the `_item` default) would collapse
    # every fold's training set for that pass — unrelated to what this test prices.
    items = [_item(i, db.LABELS[i % 3], category=f"c{i}") for i in range(6)]
    chat, _ = _fake_openai_chat({"B": math.log(0.9), "C": math.log(0.1)})
    embed, _ = _fake_openai_embeddings()
    client = SimpleNamespace(chat=chat.chat, embeddings=embed.embeddings)
    prices = {**db.PRICES, "embedding": (0.5, 0.0), "llm": (2.0, 8.0)}
    reports = db.run(["embedding", "llm"], items, tmp_path, offline=False, folds=2, seed=0,
                     client_factory=lambda _offline: client, env={}, prices=prices)
    cost = {r["arm"]: r["cost_per_1000_usd"] for r in reports}
    # embedding: 42 input tokens × 0.5 per 1M = 0.000021 a decision → 0.021 per 1,000.
    # llm: 310 × 2.0 + 1 × 8.0 = 628 per 1M = 0.000628 a decision → 0.628 per 1,000.
    assert cost == pytest.approx({"embedding": 0.021, "llm": 0.628})


def test_evaluate_passes_a_skipped_arm_through_without_scores():
    report = db.evaluate([], db.ArmResult("llm", "skipped", "no recording"), {})
    assert report == {"arm": "llm", "status": "skipped", "reason": "no recording"}


def test_cli_runs_offline_on_the_committed_set_and_never_prints_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)  # present, but --offline must win
    out = tmp_path / "report.json"
    assert db.main(["--offline", "--recordings", str(tmp_path / "rec"), "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    status = {a["arm"]: a["status"] for a in report["arms"]}
    assert status == {"heuristic": "ok", "embedding": "skipped", "llm": "skipped", "jev": "refused"}
    assert report["n_items"] >= 400
    printed = capsys.readouterr()
    assert FAKE_OPENAI_KEY not in printed.out + printed.err + out.read_text(encoding="utf-8")
    assert not (tmp_path / "rec").exists()  # nothing was recorded, because nothing was called


def test_price_overrides_replace_only_the_named_arm():
    prices = db.parse_prices(["llm=0.1,0.4"])
    assert prices["llm"] == (0.1, 0.4) and prices["jev"] == db.PRICES["jev"]
    with pytest.raises(ValueError):
        db.parse_prices(["oracle=1,1"])
    with pytest.raises(ValueError):
        db.parse_prices(["llm=0.1"])


def test_price_override_reaches_the_report(tmp_path):
    items = [_item(0, "none", answer="x" * 400)]
    [jev] = db.run(["jev"], items, tmp_path, offline=True, folds=2, seed=0, env={},
                   prices=db.parse_prices(["jev=1.0,0"]))
    tokens = len(db.LLM_SYSTEM + db.embed_input(items[0])) // 4
    assert jev["projected_cost_per_1000_usd"] == pytest.approx(tokens * 1.0 / 1e6 * 1000)


def test_cli_rejects_an_unknown_arm(tmp_path):
    with pytest.raises(ValueError):
        db.main(["--arms", "oracle", "--offline", "--out", str(tmp_path / "r.json")])
