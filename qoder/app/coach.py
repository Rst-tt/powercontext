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

"""The study loop.

One round of practice is a straight line through both technologies:

1. ``seekdb`` answers "which passages should this learner see next?" —
   vector similarity plus full-text match, fused with reciprocal-rank fusion,
   filtered by course. Material mastery and review counts live in the same
   store, so scheduling and retrieval never disagree about progress.
2. The learner answers, and the outcome becomes a fact about the *learner*:
   ``PowerContext`` records the weakness, the streak, or the stated preference.
3. The next round starts by reading that memory back
   (``context/prepare`` + ``memory/search``), so the deck, the hint text and
   the explanation order all reflect what the learner already showed.

Nothing here is a mock: every step goes through the real store or the real
PowerContext Server, and each step leaves an evidence record behind.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .config import Settings
from .embedding import Embedder
from .pc_memory import LearnerMemory, MemoryUnavailable
from .store import EvidenceLog, MaterialStore
from .text import chunk_text, keywords, squeeze, tokenize

LOGGER = logging.getLogger(__name__)

MAX_SESSIONS = 20
VERDICTS = {"correct", "hard", "again"}
MASTERY_DELTA = {"correct": 0.18, "hard": 0.05, "again": -0.28}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class SessionState:
    """In-memory deck for one practice round."""

    id: str
    course: str | None
    created_at: str
    cards: list[dict[str, Any]] = field(default_factory=list)
    memory_context: dict[str, Any] | None = None
    memory_hits: list[dict[str, Any]] = field(default_factory=list)
    preference: str = ""
    graded: int = 0


@dataclass(slots=True)
class Coach:
    """Application service shared by the HTTP layer and the smoke test."""

    settings: Settings
    embedder: Embedder
    store: MaterialStore
    memory: LearnerMemory
    mirror_note: str = ""
    sessions: OrderedDict[str, SessionState] = field(default_factory=OrderedDict)

    # -- lifecycle -----------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.store.describe(),
            "backend_note": self.mirror_note,
            "embedding": {"name": self.embedder.name, "dim": self.embedder.dim},
            "memory": self.memory.describe(),
            "stats": self.store.stats(),
            "settings": self.settings.describe(),
        }

    # -- material import -----------------------------------------------------
    async def import_text(
        self,
        *,
        course: str,
        title: str,
        text: str,
        source: str = "",
        passage_size: int = 420,
    ) -> dict[str, Any]:
        """Split one note into passages and store them for retrieval."""
        course = squeeze(course) or "未分类"
        title = squeeze(title) or "未命名资料"
        body = squeeze(text)
        if not body:
            raise ValueError("资料内容为空")
        passages = chunk_text(body, size=passage_size, overlap=80) or [body]
        materials = []
        for index, passage in enumerate(passages, start=1):
            materials.append(
                {
                    "id": f"m_{uuid4().hex[:12]}",
                    "course": course,
                    "chapter": f"第 {index} 段" if len(passages) > 1 else "",
                    "title": f"{title} · {index}" if len(passages) > 1 else title,
                    "body": passage,
                    "keywords": keywords(f"{title} {passage}", limit=8),
                    "difficulty": 2,
                    "source": squeeze(source),
                    "mastery": 0.0,
                    "reviews": 0,
                    "last_verdict": "",
                    "updated_at": _now(),
                }
            )
        written = await asyncio.to_thread(self.store.upsert_materials, materials)
        return {"course": course, "title": title, "passages": written, "ids": [item["id"] for item in materials]}

    async def seed_samples(self) -> dict[str, Any]:
        """Load the bundled study notes once (idempotent per note title)."""
        from .samples import SAMPLES

        existing = {
            (str(item["course"]), str(item["title"]).split(" · ")[0])
            for item in await asyncio.to_thread(self.store.materials, None, 500)
        }
        total = 0
        skipped = 0
        for sample in SAMPLES:
            if (sample["course"], sample["title"]) in existing:
                skipped += 1
                continue
            result = await self.import_text(
                course=sample["course"],
                title=sample["title"],
                text=sample["text"],
                source="samples",
            )
            total += int(result["passages"])
        return {"imported": total, "skipped": skipped}

    # -- retrieval -----------------------------------------------------------
    async def search(self, query: str, course: str | None = None, top_k: int = 6) -> dict[str, Any]:
        hits, trace = await asyncio.to_thread(self.store.hybrid_search, query, course, top_k)
        return {
            "query": query,
            "course": course,
            "hits": [hit.to_payload() for hit in hits],
            "trace": [entry.to_payload() for entry in trace],
        }

    def _memory_overlap(self, material: dict[str, Any], weakness_text: str) -> float:
        """Cheap affinity between a passage and the learner's recorded weak spots."""
        if not weakness_text:
            return 0.0
        weak_tokens = set(tokenize(weakness_text))
        if not weak_tokens:
            return 0.0
        material_tokens = set(tokenize(f"{material['title']} {material['body']}")) | set(material.get("keywords") or [])
        if not material_tokens:
            return 0.0
        return len(weak_tokens & material_tokens) / max(1, min(len(weak_tokens), len(material_tokens)))

    async def _read_memory(self, course: str | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        """Ask PowerContext what it remembers before the deck is built.

        The entries list is the authoritative source: memory search ranks by
        relevance to a short query, while the learner's recorded weak spots are
        exactly the rows that should reorder the deck. Search results are added
        as extra hints when they say something new.
        """
        if not self.memory.available:
            return None, [], ""
        focus = f"{course or ''} 还没记牢 薄弱点 概念 复习".strip()
        prepared: dict[str, Any] | None = None
        hints: list[dict[str, Any]] = []
        preference = ""
        try:
            prepared = await self.memory.prepare(focus)
        except MemoryUnavailable:
            LOGGER.warning("context/prepare failed; continuing without prepared context")
        try:
            entries = await self.memory.entries()
        except MemoryUnavailable:
            entries = []
            LOGGER.warning("memory/entries/list failed; preference stays empty")
        for entry in entries:
            if entry.get("state") != "active":
                continue
            kind = str(entry.get("kind") or "")
            text = str(entry.get("text") or "")
            if not text:
                continue
            if kind == "preference":
                preference = text
            elif kind in ("weakness", "misconception"):
                hints.append({"text": text, "kind": kind})
        try:
            hints.extend(await self.memory.search(focus, limit=6))
        except MemoryUnavailable:
            LOGGER.warning("memory/search failed; continuing without search hints")
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for hint in hints:
            text = str(hint.get("text") or "")
            if text and text not in seen:
                seen.add(text)
                unique.append(hint)
        return prepared, unique[:8], preference

    # -- session start -------------------------------------------------------
    async def start_session(self, course: str | None = None, budget: int = 8) -> dict[str, Any]:
        """Build one practice deck: retrieval decides the cards, memory orders them."""
        course = squeeze(course) if course else None
        materials = await asyncio.to_thread(self.store.materials, course, 200)
        if not materials:
            raise ValueError("还没有资料。先导入一份笔记或加载示例资料。")

        prepared, memory_hits, preference = await self._read_memory(course)
        weakness_text = " ".join(str(hit.get("text") or "") for hit in memory_hits)

        def priority(material: dict[str, Any]) -> tuple[float, float, int, str]:
            affinity = self._memory_overlap(material, weakness_text)
            seen = int(material.get("reviews") or 0)
            return (-affinity, float(material.get("mastery") or 0.0), seen, material["id"])

        candidates = sorted(materials, key=priority)
        due = [item for item in candidates if float(item.get("mastery") or 0.0) < 0.72 or int(item.get("reviews") or 0) == 0]
        deck = (due or candidates)[: max(1, min(int(budget), 12))]

        distractors = self._distractor_pool(materials)
        pools: dict[str, list[str]] = {}
        for item in materials:
            pools.setdefault(str(item["course"]), []).extend(item.get("keywords") or [])
        cards: list[dict[str, Any]] = []
        for material in deck:
            pool = pools.get(str(material["course"])) or distractors
            card = self._build_card(material, pool)
            related, trace = await asyncio.to_thread(self.store.hybrid_search, card["prompt"], material["course"], 3)
            card["related"] = [
                {
                    "title": hit.material["title"],
                    "course": hit.material["course"],
                    "snippet": squeeze(hit.material["body"])[:220],
                    "fused": round(hit.fused, 6),
                    "vector_rank": (hit.vector or {}).get("rank"),
                    "lexical_rank": (hit.lexical or {}).get("rank"),
                }
                for hit in related
                if hit.material["id"] != material["id"]
            ][:2]
            card["retrieval"] = [entry.to_payload() for entry in trace]
            card["affinity"] = round(self._memory_overlap(material, weakness_text), 3)
            cards.append(card)

        session = SessionState(
            id=f"ses_{uuid4().hex[:10]}",
            course=course,
            created_at=_now(),
            cards=cards,
            memory_context=prepared,
            memory_hits=memory_hits,
            preference=preference,
        )
        self.sessions[session.id] = session
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.popitem(last=False)

        return {
            "session_id": session.id,
            "course": course,
            "cards": [_public_card(card) for card in cards],
            "memory": {
                "available": self.memory.available,
                "context": {
                    "status": (prepared or {}).get("status"),
                    "content": (prepared or {}).get("content") or "",
                    "content_bytes": (prepared or {}).get("content_bytes"),
                },
                "hits": [
                    {"text": hit.get("text"), "score": hit.get("score"), "matched_by": hit.get("matched_by")}
                    for hit in memory_hits
                ],
                "preference": preference,
            },
            "memory_evidence": self.memory.log(limit=6),
            "backend": self.store.describe(),
        }

    # -- grading -------------------------------------------------------------
    async def grade(self, session_id: str, material_id: str, verdict: str | None = None, picked: str | None = None) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError("会话已过期，请重新开始一轮复习")
        card = next((item for item in session.cards if item["material_id"] == material_id), None)
        if card is None:
            raise ValueError("这张卡片不属于当前会话")

        if picked is not None and verdict is None:
            verdict = "correct" if squeeze(picked) == squeeze(card["answer"]) else "again"
        verdict = (verdict or "").lower()
        if verdict not in VERDICTS:
            raise ValueError("verdict 必须是 correct / hard / again 之一")

        material = card["_material"]
        mastery = float(material.get("mastery") or 0.0)
        reviews = int(material.get("reviews") or 0) + 1
        new_mastery = max(0.0, min(1.0, mastery + MASTERY_DELTA[verdict]))
        await asyncio.to_thread(self.store.update_mastery, material_id, new_mastery, reviews, verdict)
        await asyncio.to_thread(
            self.store.record_review,
            {
                "id": f"rv_{uuid4().hex[:12]}",
                "material_id": material_id,
                "course": material["course"],
                "verdict": verdict,
                "question": card["prompt"][:200],
                "context_hit": (card["related"][0]["snippet"] if card.get("related") else ""),
                "created_at": _now(),
            },
        )

        memory_write: dict[str, Any] | None = None
        if self.memory.available:
            try:
                if verdict == "again":
                    text = (
                        f"《{material['course']}》{material['title']}：概念「{card['answer']}」还没记牢"
                        f"（复习第 {reviews} 次仍然答错）。"
                    )
                    # One weak-spot row per note: forget every stale wording so the
                    # panel and the prepared context do not accumulate duplicates.
                    # Legacy memory envelopes (no atomic artifact ref) cannot be
                    # retired; the new entry is still written and shown.
                    stale = await self._weaknesses_for(material)
                    for entry in stale:
                        artifact = entry.get("artifact")
                        if not artifact:
                            continue
                        await self.memory.forget(artifact, int(entry.get("state_version") or 0))
                    memory_write = await self.memory.remember(
                        "weakness",
                        text,
                        reason="复习卡片再次答错时刷新薄弱点" if stale else "复习卡片答错时自动记录薄弱点",
                    )
                elif verdict == "correct" and reviews % 3 == 0:
                    memory_write = await self.memory.remember(
                        "progress",
                        f"《{material['course']}》{material['title']}：同类卡片已连续复习 {reviews} 次且本次答对，"
                        f"掌握度 {new_mastery:.2f}。",
                        reason="连续答对时记录进度",
                    )
            except MemoryUnavailable:
                LOGGER.warning("memory write failed; review itself is still saved")

        session.graded += 1
        return {
            "session_id": session.id,
            "material_id": material_id,
            "verdict": verdict,
            "picked": picked,
            "answer": card["answer"],
            "question": card["question"],
            "related": card.get("related") or [],
            "retrieval": card.get("retrieval") or [],
            "mastery": round(new_mastery, 3),
            "reviews": reviews,
            "memory_write": memory_write,
            "remaining": max(0, len(session.cards) - session.graded),
            "backend": self.store.stats(),
        }

    # -- card construction ---------------------------------------------------
    async def _weaknesses_for(self, material: dict[str, Any]) -> list[dict[str, Any]]:
        marker = f"《{material['course']}》{material['title']}："
        try:
            entries = await self.memory.entries()
        except MemoryUnavailable:
            return []
        return [
            entry
            for entry in entries
            if entry.get("kind") == "weakness"
            and entry.get("state") == "active"
            and marker in str(entry.get("text") or "")
        ]

    @staticmethod
    def _distractor_pool(materials: list[dict[str, Any]]) -> list[str]:
        pool: list[str] = []
        for material in materials:
            for word in material.get("keywords") or []:
                if 2 <= len(word) <= 12 and word not in pool:
                    pool.append(word)
        return pool

    # Function words that make a keyword phrase a poor fill-in answer or option.
    _STOP_WORDS = (
        "的", "了", "是", "它", "这", "那", "我", "你", "他", "她", "们", "也", "就",
        "在", "把", "被", "给", "对", "与", "及", "或", "而", "但", "能", "会", "要",
        "可以", "一个", "什么", "怎么", "如果", "因为", "所以", "因此", "通过", "能够",
        "已经", "还是", "不是", "没有", "就是", "都是", "其中", "并且", "而且", "由于",
        "根据", "例如", "包括", "需要", "应该", "可能", "一定", "非常", "比较", "更加",
        "几乎", "大约", "说明", "表示", "认为", "进行", "以及", "之间", "之后", "之前",
    )

    @classmethod
    def _term_like(cls, word: str, body: str) -> bool:
        return (
            2 <= len(word) <= 12
            and word in body
            and not any(stop in word for stop in cls._STOP_WORDS)
            and not word.endswith(("的", "了", "是", "在", "和", "与", "或"))
        )

    @classmethod
    def _answer_candidates(cls, material: dict[str, Any], body: str) -> list[str]:
        # A blank is only unambiguous when the term occurs exactly once: blanking
        # one of several occurrences leaves the rest visible and gives the answer
        # away. Terms that repeat are therefore not candidates at all; without a
        # unique term the card falls back to the recitation prompt.
        words = [
            word
            for word in (material.get("keywords") or [])
            if cls._term_like(word, body) and body.count(word) == 1
        ]
        words.sort(key=lambda word: -len(word))
        return words

    @staticmethod
    def _window(marked: str, limit: int = 300) -> str:
        """Trim long notes around the blank so the card stays readable."""
        if len(marked) <= limit:
            return marked
        blank = marked.find("____")
        if blank < 0:
            return marked[:limit] + "…"
        start = max(0, blank - limit // 2)
        end = min(len(marked), start + limit)
        return ("…" if start else "") + marked[start:end] + ("…" if end < len(marked) else "")

    def _build_card(self, material: dict[str, Any], pool: list[str]) -> dict[str, Any]:
        body = squeeze(material["body"])
        candidates = self._answer_candidates(material, body)
        answer = candidates[0] if candidates else ""
        if answer:
            prompt = self._window(body.replace(answer, "____", 1))
            question = f"补全《{material['course']}》笔记中的空缺："
        else:
            prompt = self._window(body)
            question = f"用自己的话复述《{material['course']}》这条笔记的核心结论："
            answer = squeeze(material["title"])

        options = [answer]
        for word in pool:
            if len(options) >= 4:
                break
            if word == answer or word in options or not self._term_like(word, body):
                continue
            if abs(len(word) - len(answer)) <= 3 and not re.search(r"[\s]", word):
                options.append(word)
        if len(options) < 4:
            for word in ("定义", "示例", "结论", "条件"):
                if len(options) >= 4:
                    break
                if word != answer and word not in options:
                    options.append(word)
        # Keep the correct answer in the options, then present a stable order.
        stable = sorted(options, key=lambda item: (len(item), item))
        return {
            "material_id": material["id"],
            "course": material["course"],
            "title": material["title"],
            "question": question,
            "prompt": prompt,
            "options": stable,
            "answer": answer,
            "keywords": material.get("keywords") or [],
            "_material": material,
        }

    # -- memory maintenance (explicit learner actions) -----------------------
    async def set_preference(self, value: str) -> dict[str, Any]:
        text = squeeze(value)[:200]
        if not text:
            raise ValueError("偏好内容为空")
        if not self.memory.available:
            raise MemoryUnavailable("PowerContext 不可用，无法保存偏好")
        # Keep a single active preference: forget the previous wording so the
        # panel and the prepared context do not accumulate near-duplicates.
        try:
            for entry in await self.memory.entries():
                if entry.get("kind") == "preference" and entry.get("state") == "active":
                    artifact = entry.get("artifact")
                    if not artifact:
                        # Legacy envelope: no lifecycle handle, so the old
                        # wording stays active; writing the new one still wins.
                        continue
                    await self.memory.forget(artifact, int(entry.get("state_version") or 0))
        except MemoryUnavailable:
            LOGGER.warning("could not retire previous preference; writing the new one anyway")
        return await self.memory.remember("preference", f"讲解偏好：{text}", reason="学习者在设置里显式保存")

    async def revise_entry(self, artifact: dict[str, Any], state_version: int, kind: str, text: str) -> dict[str, Any]:
        await self.memory.forget(artifact, state_version)
        return await self.memory.remember(kind, text, reason="学习者在记忆面板中修正")

    async def retire_entry(self, artifact: dict[str, Any], state_version: int, reason: str) -> dict[str, Any]:
        return await self.memory.forget(artifact, state_version)

    async def memory_snapshot(self) -> dict[str, Any]:
        if not self.memory.available:
            return {"available": False, "error": self.memory.error, "entries": [], "evidence": self.memory.log()}
        entries = await self.memory.entries()
        preference = next(
            (str(entry.get("text")) for entry in entries if entry.get("kind") == "preference" and entry.get("state") == "active"),
            "",
        )
        return {
            "available": True,
            "scope_id": self.memory.scope_id,
            "base_url": self.memory.base_url,
            "entries": entries,
            "preference": preference,
            "evidence": self.memory.log(),
        }


def _public_card(card: dict[str, Any]) -> dict[str, Any]:
    """Send a card to the browser without leaking the answer or the answer panel."""
    return {
        "material_id": card["material_id"],
        "course": card["course"],
        "title": card["title"],
        "question": card["question"],
        "prompt": card["prompt"],
        "options": card["options"],
        "keywords": card["keywords"],
        "affinity": card["affinity"],
    }
