"""Адаптер checkpointer'а LangGraph на Valkey (redis-py) — ruling R2 ARCH-GATE S08.

Сессия уточняющего вопроса (interrupt/resume — S00 стык 1): state прогона
сериализуется сериализатором LangGraph (JsonPlusSerializer — pydantic-VO из
state поддержаны) и хранится в Valkey с TTL ``ttl_s`` (канон 10 мин —
Settings.clarify_session_ttl_s; каждый SETEX продлевает TTL — «сессия живёт
10 минут от последней записи», брошенная сессия исчезает сама).

Семантика хранения — зеркало MemorySaver (референс langgraph-checkpoint 4.x):
чекпоинт с channel_values целиком + метаданные + parent_id в одном блобе,
pending-writes по (task_id, idx) с first-wins (кроме спец-каналов
WRITES_IDX_MAP), выборка «последний чекпоинт» через индексный ключ. channel_values
не разносится по блобам-версиям (оптимизация MemorySaver для инкрементальной
выдачи) — state прогона мал (КБ), полный блоб проще и дешевле по числу ключей.

TTL истёк → ключей нет → get_tuple → None → раннер синтезирует
clarification-терминал (ruling R3 ARCH-GATE S08 — валидный исход, не исключение).
Асинхронные методы — делегирование в синхронные (прецедент MemorySaver; async-
инвокация графа вне скоупа MVP — N1).
"""

import random
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from functools import wraps
from typing import Any

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)
from redis import Redis
from redis.exceptions import RedisError

from text_to_sql_agent.application.ports.checkpointer import CheckpointStoreError

# Префикс ключей в Valkey: пространство имён чекпоинтов агента (клиент кэша
# вопросов S16 и прочие потребители живут в других префиксах)
_KEY_PREFIX = "ckptr"
# Разделитель kind/данных сериализатора в блобе (US — не встречается в kind)
_KIND_SEP = b"\x1f"


def _pack(typed: tuple[str, bytes]) -> bytes:
    kind, data = typed
    return kind.encode("ascii") + _KIND_SEP + data


def _unpack(raw: bytes) -> tuple[str, bytes]:
    kind, _, data = raw.partition(_KIND_SEP)
    return kind.decode("ascii"), data


def _store_guard(method: Callable[..., Any]) -> Callable[..., Any]:
    """Граница ошибок хранилища: сырые redis-сбои → CheckpointStoreError (стык).

    S00 правило 8 + ревью S08 Important-2: сбой Valkey не должен выходить из
    FSM traceback-ом — раннер ловит типизированную ошибку стыка и переводит
    в валидный fail-исход (деталь — операторскому трейсу, S9).
    """

    @wraps(method)
    def wrapper(self: "ValkeyCheckpointer", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except RedisError as exc:
            raise CheckpointStoreError(f"хранилище сессий недоступно: {exc}") from exc

    return wrapper


class ValkeyCheckpointer(BaseCheckpointSaver[str]):
    """Checkpointer на Valkey: сессии interrupt-прогонов с TTL (S00 стек — redis-py)."""

    def __init__(self, url: str, *, ttl_s: int) -> None:
        super().__init__()
        self._client = Redis.from_url(url, decode_responses=False)
        self._ttl_s = ttl_s  # канон 10 мин (S00 стык 1); инъекция короткого — тесты

    # --- ключи: {prefix}:{thread_id}:{ns}:{segment} ---

    @staticmethod
    def _base(thread_id: str, checkpoint_ns: str) -> str:
        return f"{_KEY_PREFIX}:{thread_id}:{checkpoint_ns or ''}"

    def _ckpt_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._base(thread_id, checkpoint_ns)}:ckpt:{checkpoint_id}"

    def _latest_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._base(thread_id, checkpoint_ns)}:latest"

    def _write_key(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str, task_id: str, idx: int
    ) -> str:
        return f"{self._base(thread_id, checkpoint_ns)}:wr:{checkpoint_id}:{task_id}:{idx}"

    # --- контракт BaseCheckpointSaver ---

    @_store_guard
    def put(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        # channel_values остаются внутри чекпоинта (см. докстринг модуля)
        blob = self.serde.dumps_typed(
            (
                checkpoint,
                get_checkpoint_metadata(config, metadata),
                config["configurable"].get("checkpoint_id"),
            )
        )
        pipe = self._client.pipeline()
        pipe.set(
            self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id), _pack(blob), ex=self._ttl_s
        )
        pipe.set(self._latest_key(thread_id, checkpoint_ns), checkpoint_id, ex=self._ttl_s)
        pipe.execute()
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    @_store_guard
    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        if not checkpoint_id:
            raw_latest = self._client.get(self._latest_key(thread_id, checkpoint_ns))
            checkpoint_id = raw_latest.decode("ascii") if raw_latest else None
        if not checkpoint_id:
            return None
        raw = self._client.get(self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id))
        if raw is None:
            return None  # TTL истёк (или удалён) — сессии нет (ruling R3)
        checkpoint, metadata, parent_id = self.serde.loads_typed(_unpack(raw))
        thread_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        return CheckpointTuple(
            config=thread_config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
            pending_writes=self._pending_writes(thread_id, checkpoint_ns, checkpoint_id),
        )

    @_store_guard
    def put_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        if not checkpoint_id:
            raw_latest = self._client.get(self._latest_key(thread_id, checkpoint_ns))
            checkpoint_id = raw_latest.decode("ascii") if raw_latest else ""
        for idx, (channel, value) in enumerate(writes):
            inner_idx = WRITES_IDX_MAP.get(channel, idx)
            key = self._write_key(thread_id, checkpoint_ns, checkpoint_id, task_id, inner_idx)
            # first-wins по (task_id, idx) — как MemorySaver; спец-каналы
            # (WRITES_IDX_MAP: -1) перезаписываются всегда
            if inner_idx >= 0 and self._client.exists(key):
                continue
            self._client.set(
                key,
                _pack(self.serde.dumps_typed((task_path, channel, value))),
                ex=self._ttl_s,
            )

    @_store_guard
    def _pending_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        pattern = f"{self._base(thread_id, checkpoint_ns)}:wr:{checkpoint_id}:*"
        entries: list[tuple[str, int, str, tuple[str, str, Any]]] = []
        for key in self._client.scan_iter(match=pattern):
            suffix = key.decode("ascii").rsplit(":", 2)  # [.., task_id, idx]
            task_id, idx = suffix[1], int(suffix[2])
            raw = self._client.get(key)
            if raw is None:  # истёк между SCAN и GET — пропускаем
                continue
            task_path, channel, value = self.serde.loads_typed(_unpack(raw))
            entries.append((task_path, task_id, idx, (task_id, channel, value)))
        entries.sort(key=lambda e: writes_sort_key(e[0], e[1], e[2]))
        return [entry[3] for entry in entries]

    @_store_guard
    def list(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        # Материализация (не генератор): guard-декоратор ловит redis-сбои тела,
        # а тело генератора исполняется лениво — мимо try/except обёртки
        if config is None:
            return iter(())  # по-потоковая история вне скоупа (MVP — один thread)
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        pattern = f"{self._base(thread_id, checkpoint_ns)}:ckpt:*"
        ids: list[str] = [
            key.decode("ascii").rsplit(":", 1)[1] for key in self._client.scan_iter(match=pattern)
        ]
        before_id = get_checkpoint_id(before) if before else None
        tuples: list[CheckpointTuple] = []
        remaining = limit
        for checkpoint_id in sorted(ids, reverse=True):
            if before_id and checkpoint_id >= before_id:
                continue
            tuple_ = self.get_tuple(
                {"configurable": {**config["configurable"], "checkpoint_id": checkpoint_id}}
            )
            if tuple_ is None:
                continue
            if filter and not all(tuple_.metadata.get(k) == v for k, v in filter.items()):
                continue
            if remaining is not None:
                if remaining <= 0:
                    break
                remaining -= 1
            tuples.append(tuple_)
        return iter(tuples)

    @_store_guard
    def delete_thread(self, thread_id: str) -> None:
        pattern = f"{_KEY_PREFIX}:{thread_id}:*"
        keys = list(self._client.scan_iter(match=pattern))
        if keys:
            self._client.delete(*keys)

    # --- async: делегирование в sync (прецедент MemorySaver; async-инвокация — N1) ---

    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return self.put_writes(config, writes, task_id, task_path)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        return self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        # Каноническая схема версий каналов MemorySaver (монотонный счётчик +
        # случайный хвост — уникальность версии на ветке)
        if current is None:
            current_v = 0
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    def close(self) -> None:
        """Закрыть пул соединений (CLI вызывает в finally — гигиена ресурсов)."""
        try:
            self._client.close()
        except RedisError:  # гигиена: сбой закрытия не роняет процесс
            pass
