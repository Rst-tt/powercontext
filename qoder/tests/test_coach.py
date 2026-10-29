# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio

import pytest

from app.coach import MASTERY_DELTA, Coach, _public_card
from app.embedding import HashEmbedder
from app.pc_memory import MemoryUnavailable

NOTE = (
    "虚拟内存用页表把进程的虚拟地址映射到物理页框，每个进程都以为自己拥有连续的地址空间。"
    "缺页中断发生时，如果内存已满，就要按页面置换算法淘汰一个页面。"
    "FIFO 简单但会出现 Belady 异常，即分配更多页框反而缺页更多；LRU 近似最近未使用，"
    "但完整实现代价高，实际多用时钟算法近似。页面置换算法的评价指标是缺页率，"
    "测试时用固定引用串比较各算法的缺页次数。"
)


def _coach(settings, store, memory) -> Coach:
    return Coach(settings=settings, embedder=HashEmbedder(settings.embedding_dim), store=store, memory=memory)


def test_import_text_splits_long_notes(settings, store) -> None:
    coach = _coach(settings, store, memory=None)
    result = asyncio.run(coach.import_text(course="操作系统", title="虚拟内存", text=NOTE * 3))
    assert result["passages"] >= 2
    assert len(store.materials("操作系统")) == result["passages"]


def test_seed_samples_is_idempotent(settings, store) -> None:
    from app.samples import SAMPLES

    coach = _coach(settings, store, memory=None)
    first = asyncio.run(coach.seed_samples())
    second = asyncio.run(coach.seed_samples())
    assert first["imported"] >= len(SAMPLES)
    assert second["imported"] == 0
    assert second["skipped"] == len(SAMPLES)


def test_start_session_builds_private_cards(settings, store, memory) -> None:
    coach = _coach(settings, store, memory)
    asyncio.run(coach.import_text(course="操作系统", title="虚拟内存", text=NOTE))
    payload = asyncio.run(coach.start_session(course="操作系统", budget=4))
    assert payload["memory"]["available"] is True
    assert payload["cards"]
    assert len(payload["cards"]) <= 4
    public = payload["cards"][0]
    assert "answer" not in public
    private = coach.sessions[payload["session_id"]].cards[0]
    assert private["answer"] in public["options"]
    assert len(public["options"]) == 4


def test_grade_correct_raises_mastery(settings, store, memory) -> None:
    coach = _coach(settings, store, memory)
    asyncio.run(coach.import_text(course="操作系统", title="虚拟内存", text=NOTE))
    session = asyncio.run(coach.start_session(course="操作系统", budget=1))
    card = coach.sessions[session["session_id"]].cards[0]
    result = asyncio.run(
        coach.grade(session["session_id"], card["material_id"], picked=card["answer"])
    )
    assert result["verdict"] == "correct"
    assert result["mastery"] == pytest.approx(MASTERY_DELTA["correct"])
    stored = {row["id"]: row for row in store.materials("操作系统")}[card["material_id"]]
    assert stored["reviews"] == 1


def test_wrong_answer_dedupes_weakness_memory(settings, store, memory) -> None:
    coach = _coach(settings, store, memory)
    asyncio.run(coach.import_text(course="操作系统", title="虚拟内存", text=NOTE))
    session = asyncio.run(coach.start_session(course="操作系统", budget=1))
    card = coach.sessions[session["session_id"]].cards[0]
    wrong = next(option for option in card["options"] if option != card["answer"])
    first = asyncio.run(coach.grade(session["session_id"], card["material_id"], picked=wrong))
    assert first["verdict"] == "again"
    assert first["memory_write"]["entry"]["kind"] == "weakness"
    asyncio.run(coach.grade(session["session_id"], card["material_id"], picked=wrong))
    active = [entry for entry in memory._entries if entry["kind"] == "weakness" and entry["state"] == "active"]
    assert len(active) == 1


def test_offline_memory_keeps_review_flow(settings, store, offline_memory) -> None:
    coach = _coach(settings, store, offline_memory)
    asyncio.run(coach.import_text(course="操作系统", title="虚拟内存", text=NOTE))
    session = asyncio.run(coach.start_session(course="操作系统", budget=1))
    assert session["memory"]["available"] is False
    card = coach.sessions[session["session_id"]].cards[0]
    wrong = next(option for option in card["options"] if option != card["answer"])
    result = asyncio.run(coach.grade(session["session_id"], card["material_id"], picked=wrong))
    assert result["verdict"] == "again"
    assert result["memory_write"] is None
    with pytest.raises(MemoryUnavailable):
        asyncio.run(coach.set_preference("先给反例"))


def test_preference_and_revise_replace_old_entry(settings, store, memory) -> None:
    coach = _coach(settings, store, memory)
    asyncio.run(coach.set_preference("先给反例"))
    asyncio.run(coach.set_preference("先给定义"))
    active = [entry for entry in memory._entries if entry["kind"] == "preference" and entry["state"] == "active"]
    assert len(active) == 1
    assert "先给定义" in active[0]["text"]

    rewritten = asyncio.run(coach.revise_entry(active[0]["artifact"], active[0]["state_version"], "preference", "讲解偏好：先给结论"))
    assert rewritten["entry"]["state"] == "active"
    assert active[0]["state"] == "forgotten"


def test_card_falls_back_to_title_without_terms(settings, store) -> None:
    coach = _coach(settings, store, memory=None)
    material = {
        "id": "m_x",
        "course": "操作系统",
        "title": "复习要点",
        "body": "这是一段没有可用术语的说明。",
        "keywords": [],
        "mastery": 0.0,
        "reviews": 0,
    }
    card = coach._build_card(material, [])
    assert card["answer"] == "复习要点"
    assert len(card["options"]) == 4
    assert "____" not in card["prompt"]
    card["affinity"] = 0.0
    assert "_material" in card and "answer" not in _public_card(card)


def test_only_unique_terms_are_blanked(settings, store) -> None:
    coach = _coach(settings, store, memory=None)

    repeated = {
        "id": "m_repeat",
        "course": "操作系统",
        "title": "进程与线程",
        "body": "进程是资源分配的基本单位，进程也需要调度。",
        "keywords": ["进程"],
        "mastery": 0.0,
        "reviews": 0,
    }
    card = coach._build_card(repeated, [])
    assert "____" not in card["prompt"]
    assert card["answer"] == "进程与线程"

    unique = {
        "id": "m_unique",
        "course": "操作系统",
        "title": "分页",
        "body": "页表把进程的虚拟地址映射到物理页框。",
        "keywords": ["页表"],
        "mastery": 0.0,
        "reviews": 0,
    }
    card = coach._build_card(unique, [])
    assert "____" in card["prompt"]
    assert card["answer"] == "页表"
    assert card["answer"] not in card["prompt"]


def test_start_session_without_materials_errors(settings, store) -> None:
    coach = _coach(settings, store, memory=None)
    with pytest.raises(ValueError):
        asyncio.run(coach.start_session())


def test_grade_rejects_unknown_session(settings, store) -> None:
    coach = _coach(settings, store, memory=None)
    with pytest.raises(ValueError):
        asyncio.run(coach.grade("ses_missing", "m_1", verdict="correct"))
